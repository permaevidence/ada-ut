#!/usr/bin/env python3
"""Battery for py/release_verify.py — the app's signed-release verifier.

Covers: the pure-Python Ed25519 verifier (RFC 8032 known answers, strict
encodings, cross-check against an independent OpenSSL over fresh random
signatures), the system-OpenSSL provider, the full envelope/manifest
rejection matrix mirrored from the Ada CLI's verifier, the locked
monotonic anti-rollback store (concurrent writers, lock contention,
corrupt/legacy files, domain isolation), bounded fetches and authenticated
streaming downloads against a lying server, and end-to-end resolution.

    python3 scripts/release_selftest.py
"""

import base64
import hashlib
import json
import multiprocessing
import os
import secrets
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "py"))
import release_verify as rv  # noqa: E402
from signing_fixture import TestKey, FakeHost, manifest_bytes  # noqa: E402

PASSED = FAILED = 0


def check(label, ok, detail=""):
    global PASSED, FAILED
    print("  %s %s%s" % ("✔" if ok else "✖", label,
                          "" if ok or not detail else " — " + str(detail)[:300]))
    if ok:
        PASSED += 1
    else:
        FAILED += 1


def expect(kind, fn, label):
    try:
        fn()
    except rv.ReleaseVerifyError as exc:
        check(label, exc.kind == kind, "kind=%s: %s" % (exc.kind, exc))
        return
    check(label, False, "accepted")


def resolve_openssl():
    script = os.path.join(HERE, "release", "openssl-resolve.sh")
    out = subprocess.run(
        ["bash", "-c", '. "%s"; resolve_openssl && printf %%s "$OPENSSL"' % script],
        capture_output=True, text=True)
    return out.stdout.strip() or None


def _trust_writer(args):
    trust_file, domain, seq = args
    import release_verify as mod
    mod.TRUST_FILE = trust_file
    mod.trust_record(domain, seq)
    return mod.trust_floor(domain)[0]


def _lock_holder(args):
    trust_file, hold = args
    import fcntl
    fd = os.open(trust_file + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    time.sleep(hold)
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def main():
    root = tempfile.mkdtemp(prefix="ada-ut-release-selftest-")
    rv.TRUST_FILE = os.path.join(root, "trust", "release_trust.json")
    now = 1_800_000_000.0  # 2027-01-15 — fixtures expire 2099, published 2026
    ossl = resolve_openssl()
    check("an Ed25519-capable openssl is available for cross-checks", bool(ossl), "none")

    # ------------------------------------------------ 1. Ed25519 core
    print("— pure-Python Ed25519 —")
    check("RFC 8032 known-answer vectors pass (python)",
          rv._provider_passes_vectors(rv.ed25519_verify_python))
    check("L·B is the identity", rv._pt_equal(rv._pt_mul(rv._L, rv._B), rv._IDENTITY))
    pub1, _, sig1 = rv.KNOWN_VECTORS[0]
    pub1, sig1 = bytes.fromhex(pub1), bytes.fromhex(sig1)
    check("wrong signature length rejected", not rv.ed25519_verify_python(pub1, sig1[:-1], b""))
    check("wrong key length rejected", not rv.ed25519_verify_python(pub1[:-1], sig1, b""))
    s_plus_l = int.from_bytes(sig1[32:], "little") + rv._L
    check("non-canonical scalar s+L rejected (malleability)",
          not rv.ed25519_verify_python(pub1, sig1[:32] + s_plus_l.to_bytes(32, "little"), b""))
    y_ge_p = (rv._P + 1).to_bytes(32, "little")
    check("non-canonical point encoding y>=p rejected", rv._pt_decode(y_ge_p) is None)
    check("x=0 with sign bit set rejected",
          rv._pt_decode((1 | (1 << 255)).to_bytes(32, "little")) is None)
    check("non-bytes inputs rejected, no exception",
          not rv.ed25519_verify_python("x", sig1, b"") and not rv.ed25519_verify_python(pub1, None, b""))
    if ossl:
        work = tempfile.mkdtemp(dir=root)
        subprocess.run([ossl, "genpkey", "-algorithm", "ed25519", "-out", work + "/k.pem"],
                       check=True, capture_output=True)
        pub = subprocess.run([ossl, "pkey", "-in", work + "/k.pem", "-pubout", "-outform", "DER"],
                             check=True, capture_output=True).stdout[-32:]
        agree = tampered = 0
        n = 40
        for i in range(n):
            msg = secrets.token_bytes(secrets.randbelow(4096) + 1)
            with open(work + "/m", "wb") as f:
                f.write(msg)
            subprocess.run([ossl, "pkeyutl", "-sign", "-rawin", "-inkey", work + "/k.pem",
                            "-in", work + "/m", "-out", work + "/s"], check=True, capture_output=True)
            with open(work + "/s", "rb") as f:
                sig = f.read()
            agree += rv.ed25519_verify_python(pub, sig, msg) and rv.ed25519_verify_openssl(ossl, pub, sig, msg)
            bad_msg = msg[:-1] + bytes([msg[-1] ^ 0x01])
            bad_sig = bytes([sig[0] ^ 0x01]) + sig[1:]
            bad_pub = bytes([pub[3] ^ 0x01]) + pub[1:3] + pub[0:1] + pub[4:]
            tampered += (not rv.ed25519_verify_python(pub, sig, bad_msg)
                         and not rv.ed25519_verify_python(pub, bad_sig, msg)
                         and not rv.ed25519_verify_python(bad_pub, sig, msg)
                         and not rv.ed25519_verify_openssl(ossl, pub, bad_sig, msg))
        check("python and OpenSSL agree on %d fresh OpenSSL signatures" % n, agree == n, agree)
        check("both reject every tampered message/signature/key", tampered == n, tampered)
        check("openssl provider passes the known-answer gate",
              rv._provider_passes_vectors(lambda p, s, m: rv.ed25519_verify_openssl(ossl, p, s, m)))
        t0 = time.time()
        rv.ed25519_verify_python(pub, sig, msg)
        check("one python verification is fast enough for a phone (<250 ms here)",
              time.time() - t0 < 0.25, "%.3fs" % (time.time() - t0))
    kind, path = rv.provider()
    check("a provider was selected and proven", kind in ("openssl", "python"), kind)
    status = rv.provider_status()
    check("provider_status reports it", status["ok"] and status["provider"] == kind)

    # ------------------------------------------------ 2. envelope matrix
    print("— envelope + manifest matrix —")
    cli_key = TestKey("ada-cli")
    app_key = TestKey("ada-ut")
    prefix = "https://example.invalid/dl/v{version}/"
    policy = rv.ReleasePolicy("ada-cli", cli_key.keys(),
                              "https://example.invalid/latest/manifest.sig.json", prefix, 1)
    plat = {"linux-arm64": {"url": "https://example.invalid/dl/v1.2.3/ada-linux-arm64.tar.gz",
                            "size": 12345, "sha256": "ab" * 32}}
    good_manifest = manifest_bytes("ada-cli", "1.2.3", 7, plat)
    good = cli_key.sign(good_manifest)
    m = rv.verify_envelope(good, policy, now)
    check("valid envelope verifies", m["version"] == "1.2.3" and m["sequence"] == 7
          and m["keyId"] == cli_key.key_id and m["platforms"]["linux-arm64"]["filename"] == "ada-linux-arm64.tar.gz")
    env = json.loads(good)

    def mutated(**changes):
        e = dict(env)
        e.update(changes)
        return json.dumps(e).encode()
    payload_raw = base64.b64decode(env["payload"])
    tampered_payload = base64.b64encode(payload_raw.replace(b'"sequence": 7', b'"sequence": 8')).decode()
    expect("bad-signature", lambda: rv.verify_envelope(mutated(payload=tampered_payload), policy, now),
           "altered payload byte → bad-signature")
    sig_raw = bytearray(base64.b64decode(env["signature"]))
    sig_raw[5] ^= 1
    expect("bad-signature", lambda: rv.verify_envelope(mutated(signature=base64.b64encode(bytes(sig_raw)).decode()), policy, now),
           "altered signature → bad-signature")
    expect("unknown-key", lambda: rv.verify_envelope(mutated(keyId="ada-cli-release-v1-0000000000000000"), policy, now),
           "unknown keyId refused")
    expect("bad-signature", lambda: rv.verify_envelope(mutated(keyId=cli_key.key_id), rv.ReleasePolicy(
        "ada-cli", {cli_key.key_id: app_key.pub_hex}, policy.envelope_url, prefix, 1), now),
           "right keyId, wrong pinned key → bad-signature")
    expect("unsupported-format", lambda: rv.verify_envelope(mutated(format="ada-release-envelope-v2"), policy, now),
           "unsupported format refused")
    expect("wrong-channel", lambda: rv.verify_envelope(mutated(channel="ada-ut"), policy, now),
           "envelope channel mismatch refused")
    cross = app_key.sign(manifest_bytes("ada-ut", "1.2.3", 7, plat))
    expect("wrong-channel", lambda: rv.verify_envelope(cross, policy, now),
           "cross-channel envelope (app key, ada-ut channel) refused by the CLI policy")
    relabelled = json.loads(cross)
    relabelled["channel"] = "ada-cli"
    relabelled["keyId"] = cli_key.key_id
    expect("bad-signature", lambda: rv.verify_envelope(json.dumps(relabelled).encode(), policy, now),
           "relabelled cross-channel envelope → bad-signature (domain separation)")
    expect("malformed-envelope", lambda: rv.verify_envelope(mutated(payload=env["payload"] + " "), policy, now),
           "non-strict base64 payload refused")
    expect("malformed-envelope", lambda: rv.verify_envelope(mutated(payload=env["payload"].rstrip("=")), policy, now),
           "non-canonical base64 (missing padding) refused")
    expect("malformed-envelope", lambda: rv.verify_envelope(mutated(signature=base64.b64encode(b"x" * 63).decode()), policy, now),
           "63-byte signature refused")
    expect("too-large", lambda: rv.verify_envelope(good + b" " * (rv.MAX_ENVELOPE_BYTES), policy, now),
           "envelope over 128 KiB refused before parsing")
    big_payload = base64.b64encode(b"{" + b" " * (rv.MAX_PAYLOAD_BYTES) + b"}").decode()
    expect("too-large", lambda: rv.verify_envelope(mutated(payload=big_payload), policy, now),
           "payload over 64 KiB refused before signature work")
    expect("malformed-envelope", lambda: rv.verify_envelope(b"not json", policy, now), "non-JSON envelope refused")
    expect("malformed-envelope", lambda: rv.verify_envelope(b"[1,2]", policy, now), "non-object envelope refused")
    expect("no-keys", lambda: rv.verify_envelope(good, rv.ReleasePolicy("ada-cli", {}, "", prefix, 1), now),
           "policy without keys refuses everything")

    def signed(version="1.2.3", sequence=7, platforms=None, **kw):
        return cli_key.sign(manifest_bytes("ada-cli", version, sequence,
                                           plat if platforms is None else platforms, **kw))
    expect("malformed-manifest", lambda: rv.verify_envelope(signed(schema=2), policy, now), "schema 2 refused")
    expect("malformed-manifest", lambda: rv.verify_envelope(signed(schema=True), policy, now), "schema true (bool) refused")
    expect("wrong-channel", lambda: rv.verify_envelope(
        cli_key.sign(manifest_bytes("ada-ut", "1.2.3", 7, plat), channel="ada-cli"), policy, now),
        "payload channel mismatch refused even with a valid signature")
    expect("malformed-manifest", lambda: rv.verify_envelope(signed(sequence=0), policy, now), "sequence 0 refused")
    expect("malformed-manifest", lambda: rv.verify_envelope(signed(sequence=True), policy, now), "sequence true refused")
    expect("malformed-manifest", lambda: rv.verify_envelope(signed(sequence="7"), policy, now), "string sequence refused")
    for bad_version in ("1.2", "v1.2.3", "1.2.3-rc1", "1.2.3.4", ""):
        expect("malformed-manifest", lambda v=bad_version: rv.verify_envelope(signed(version=v), policy, now),
               "version %r refused" % bad_version)
    expect("expired", lambda: rv.verify_envelope(signed(expires="2026-12-31T00:00:00Z"), policy, now), "expired metadata refused")
    expect("not-yet-valid", lambda: rv.verify_envelope(signed(published="2027-03-01T00:00:00Z"), policy, now),
           "published far in the future refused (clock diagnostic)")
    within_skew = rv.parse_iso8601("2027-01-15T00:00:00Z", "x")
    check("published within the 24h skew allowance accepted",
          rv.verify_envelope(signed(published="2027-01-15T10:00:00Z"), policy, within_skew)["sequence"] == 7)
    check("offset timestamps parse (+02:00)",
          rv.verify_envelope(signed(published="2026-01-01T02:00:00+02:00"), policy, now)["published"]
          == rv.parse_iso8601("2026-01-01T00:00:00Z", "x"))
    expect("malformed-manifest", lambda: rv.verify_envelope(signed(published="2026-01-01 00:00:00"), policy, now),
           "non-ISO timestamp refused")
    expect("malformed-manifest", lambda: rv.verify_envelope(signed(published="2026-02-30T00:00:00Z"), policy, now),
           "impossible date refused")
    expect("malformed-manifest", lambda: rv.verify_envelope(signed(platforms={}), policy, now), "no platforms refused")
    expect("bad-platform", lambda: rv.verify_envelope(signed(platforms={"x": {
        "url": "https://evil.invalid/dl/v1.2.3/a.tar.gz", "size": 1, "sha256": "ab" * 32}}), policy, now),
        "url outside the pinned location refused")
    expect("bad-platform", lambda: rv.verify_envelope(signed(platforms={"x": {
        "url": "https://example.invalid/dl/v9.9.9/a.tar.gz", "size": 1, "sha256": "ab" * 32}}), policy, now),
        "url under another version's prefix refused")
    expect("bad-platform", lambda: rv.verify_envelope(signed(platforms={"x": {
        "url": "https://example.invalid/dl/v1.2.3/sub/a.tar.gz", "size": 1, "sha256": "ab" * 32}}), policy, now),
        "url with a path component refused")
    expect("bad-platform", lambda: rv.verify_envelope(signed(platforms={"x": {
        "url": "https://example.invalid/dl/v1.2.3/", "size": 1, "sha256": "ab" * 32}}), policy, now),
        "empty asset name refused")
    expect("bad-platform", lambda: rv.verify_envelope(signed(platforms={"x": {
        "url": plat["linux-arm64"]["url"], "size": 1, "sha256": "AB" * 32}}), policy, now),
        "uppercase sha256 refused")
    expect("bad-platform", lambda: rv.verify_envelope(signed(platforms={"x": {
        "url": plat["linux-arm64"]["url"], "size": 0, "sha256": "ab" * 32}}), policy, now),
        "size 0 refused")
    expect("bad-platform", lambda: rv.verify_envelope(signed(platforms={"x": {
        "url": plat["linux-arm64"]["url"], "size": rv.MAX_ARTIFACT_BYTES + 1, "sha256": "ab" * 32}}), policy, now),
        "size over 2 GiB refused")
    expect("bad-platform", lambda: rv.verify_envelope(signed(platforms={"x": {
        "url": plat["linux-arm64"]["url"], "size": True, "sha256": "ab" * 32}}), policy, now),
        "boolean size refused")
    expect("bad-platform", lambda: rv.verify_envelope(signed(platforms={"x": "nope"}), policy, now),
           "non-object platform entry refused")

    # ------------------------------------------------ 3. trust store
    print("— anti-rollback trust store —")
    dom_a, dom_b = "ada-cli|https://a/v{version}/", "ada-cli|https://b/v{version}/"
    check("absent file → floor 0, no note", rv.trust_floor(dom_a) == (0, None))
    rv.trust_record(dom_a, 58)
    check("recorded floor read back", rv.trust_floor(dom_a)[0] == 58)
    rv.trust_record(dom_a, 57)
    check("lower record never regresses the floor", rv.trust_floor(dom_a)[0] == 58)
    check("other domain isolated", rv.trust_floor(dom_b)[0] == 0)
    rv.trust_record(dom_b, 3)
    check("both domains coexist", rv.trust_floor(dom_a)[0] == 58 and rv.trust_floor(dom_b)[0] == 3)
    with open(rv.TRUST_FILE) as f:
        data = json.load(f)
    check("file is schema 2 with the CLI's layout", data["schema"] == 2 and data["domains"][dom_a] == 58)
    check("trust file is private (0600)", oct(os.stat(rv.TRUST_FILE).st_mode & 0o777) == "0o600")
    with open(rv.TRUST_FILE, "w") as f:
        f.write("{corrupt")
    floor, note = rv.trust_floor(dom_a)
    check("corrupt file → floor 0 with a note", floor == 0 and note and "corrupt" in note, note)
    with open(rv.TRUST_FILE, "w") as f:
        json.dump({"schema": 1, "sequence": 99}, f)
    floor, note = rv.trust_floor(dom_a)
    check("legacy v1 layout → ignored with a note", floor == 0 and note and "unrecognized" in note, note)
    rv.trust_record(dom_a, 5)
    check("record over a bad file rebuilds it (schema 2)", rv.trust_floor(dom_a) == (5, None))
    try:
        rv.trust_record(dom_a, 0)
        check("sequence 0 record rejected", False)
    except ValueError:
        check("sequence 0 record rejected", True)
    ctx = multiprocessing.get_context("fork" if sys.platform != "darwin" else "spawn")
    with ctx.Pool(8) as pool:
        results = pool.map(_trust_writer, [(rv.TRUST_FILE, dom_a, s) for s in (61, 59, 60, 63, 58, 62, 57, 64)])
    check("8 concurrent writers converge on the max", rv.trust_floor(dom_a)[0] == 64 and max(results) == 64, results)
    holder = ctx.Process(target=_lock_holder, args=((rv.TRUST_FILE, 1.2),))
    holder.start()
    time.sleep(0.3)
    t0 = time.time()
    rv.trust_record(dom_a, 70)
    waited = time.time() - t0
    holder.join()
    check("record waits for a foreign LOCK_EX holder, then completes", waited > 0.6 and rv.trust_floor(dom_a)[0] == 70,
          "%.2fs" % waited)

    # ------------------------------------------------ 4. bounded network
    print("— bounded fetch + authenticated download —")
    host = FakeHost()
    small = host.put("/small", b"x" * 100)
    check("bounded_fetch returns a body within the bound", rv.bounded_fetch(small, 100) == b"x" * 100)
    expect("too-large", lambda: rv.bounded_fetch(small, 99), "one byte over the bound is an error")
    liar = host.put("/liar", b"y" * 500, declared_length=10)
    check("a too-small Content-Length only truncates what we read (10 bytes, within bound)",
          len(rv.bounded_fetch(liar, 200)) == 10)
    liar2 = host.put("/liar2", b"y" * 500, declared_length=1000)
    expect("too-large", lambda: rv.bounded_fetch(liar2, 200), "a too-large Content-Length with a big body is still bounded")
    art = secrets.token_bytes(300_000)
    art_sha = hashlib.sha256(art).hexdigest()
    url = host.put("/v1.2.3/a.bin", art)
    dest = os.path.join(root, "dl.bin")
    progress = []
    check("exact-size, right-hash download succeeds",
          rv.download_to_file(url, dest, len(art), art_sha, progress=lambda d, t: progress.append((d, t))) is None
          and open(dest, "rb").read() == art and progress[-1] == (len(art), len(art)))
    err = rv.download_to_file(url, dest, len(art) - 1, art_sha)
    check("server sending MORE than the authenticated size is refused",
          err and "exceeded" in err and os.path.getsize(dest) <= len(art) - 1, err)
    err = rv.download_to_file(url, dest, len(art) + 1, art_sha)
    check("server sending LESS than the authenticated size is refused", err and "authenticated size" in err, err)
    err = rv.download_to_file(url, dest, len(art), "00" * 32)
    check("hash mismatch refused", err and "checksum" in err, err)
    longer = host.put("/v1.2.3/b.bin", art + b"tail", declared_length=len(art))
    err = rv.download_to_file(longer, dest, len(art), art_sha)
    check("body longer than an honest-looking Content-Length: bounded by the authenticated size",
          err is None and open(dest, "rb").read() == art, err)
    err = rv.download_to_file(host.base + "/missing", dest, 10, "00" * 32)
    check("404 reported as a download failure, not an exception", err and "download failed" in err, err)

    # ------------------------------------------------ 5. resolution
    print("— resolve_release —")
    rv.TRUST_FILE = os.path.join(root, "trust2", "release_trust.json")
    live_prefix = host.base + "/dl/v{version}/"
    live_platforms = {"linux-arm64": {"url": host.base + "/dl/v1.2.3/a.bin", "size": len(art), "sha256": art_sha}}
    env_url = host.put("/latest/manifest.sig.json", cli_key.sign(manifest_bytes("ada-cli", "1.2.3", 60, live_platforms)))
    live = rv.ReleasePolicy("ada-cli", cli_key.keys(), env_url, live_prefix, 58)
    m = rv.resolve_release(live, now)
    check("live envelope resolves with floor = embedded minimum",
          m["sequence"] == 60 and m["floor"] == 58 and m["trust_note"] is None)
    rv.record_accepted(live, m)
    check("accepted sequence persisted under the channel|prefix domain",
          rv.trust_floor(live.trust_domain)[0] == 60)
    older_platforms = {"linux-arm64": {"url": host.base + "/dl/v1.2.2/a.bin", "size": len(art), "sha256": art_sha}}
    host.put("/latest/manifest.sig.json", cli_key.sign(manifest_bytes("ada-cli", "1.2.2", 59, older_platforms)))
    expect("rollback", lambda: rv.resolve_release(live, now), "older sequence than the recorded floor → rollback")
    host.put("/latest/manifest.sig.json", cli_key.sign(manifest_bytes("ada-cli", "1.2.3", 60, live_platforms)))
    check("equal sequence (same release) still resolves", rv.resolve_release(live, now)["sequence"] == 60)
    stale = rv.ReleasePolicy("ada-cli", cli_key.keys(), env_url, live_prefix, 61)
    expect("rollback", lambda: rv.resolve_release(stale, now), "below the embedded minimum → rollback")
    other = rv.ReleasePolicy("ada-cli", cli_key.keys(), env_url, "https://other.invalid/v{version}/", 1)
    expect("bad-platform", lambda: rv.resolve_release(other, now),
           "same envelope under a different pinned location → assets outside prefix")
    check("different location has an independent floor", rv.trust_floor(other.trust_domain)[0] == 0)
    gone = rv.ReleasePolicy("ada-cli", cli_key.keys(), host.base + "/nope.json", live_prefix, 1)
    expect("unreachable", lambda: rv.resolve_release(gone, now), "missing envelope → unreachable")
    host.put("/latest/manifest.sig.json", b"{" + b" " * rv.MAX_ENVELOPE_BYTES + b"}")
    expect("too-large", lambda: rv.resolve_release(live, now), "oversized live envelope → too-large")
    host.put("/latest/manifest.sig.json", b"{}")
    expect("unsupported-format", lambda: rv.resolve_release(live, now), "unsigned/empty JSON refused")
    host.close()

    # ------------------------------------------------ 6. production pins
    print("— production pins —")
    check("CLI policy pins the Ada CLI release key",
          "ada-cli-release-v1-94d967bae0867c2e" in rv.CLI_POLICY.keys and rv.CLI_POLICY.min_sequence >= 58)
    check("app policy pins this app's release key",
          "ada-ut-release-v1-7bb0163ac16c5cb3" in rv.APP_POLICY.keys)
    committed = os.path.join(os.path.dirname(HERE), ".release-keys", "ada-ut-release.pub.pem")
    if ossl and os.path.exists(committed):
        der = subprocess.run([ossl, "pkey", "-pubin", "-in", committed, "-outform", "DER"],
                             check=True, capture_output=True).stdout
        check("pinned app key hex == committed .release-keys PEM",
              der[-32:].hex() == rv.APP_KEYS["ada-ut-release-v1-7bb0163ac16c5cb3"])
        fp = hashlib.sha256(der[-32:]).hexdigest()[:16]
        check("app keyId fingerprint matches the key", fp == "7bb0163ac16c5cb3", fp)
    check("channels point at GitHub Releases of the two public repos",
          rv.CLI_POLICY.envelope_url.startswith("https://github.com/permaevidence/ada-cli/releases/latest/")
          and rv.APP_POLICY.artifact_url_prefix.startswith("https://github.com/permaevidence/ada-ut/releases/download/v"))
    check("app build's own sequence is a positive integer ≥ the app minimum",
          isinstance(rv.APP_RELEASE_SEQUENCE, int) and rv.APP_RELEASE_SEQUENCE >= rv.MIN_APP_SEQUENCE)
    check("no environment overrides for keys/urls/openssl in release_verify",
          "os.environ" not in open(os.path.join(os.path.dirname(HERE), "py", "release_verify.py")).read())

    print("\n%d passed, %d failed" % (PASSED, FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
