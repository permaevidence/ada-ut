#!/usr/bin/env python3
"""Offline selftest for py/briglia_bridge.py's install pipeline.

Runs on any POSIX dev box (no device, no network): serves a fake SIGNED
release channel (throwaway Ed25519 keys made through scripts/release/, the
production signing scripts) from file:// URLs, with a fake `briglia` shell
script that answers --version, bundle-check and setup-api status. Covers the Codex findings of
2026-08-27: staged setup-api/schema validation BEFORE mutation, the
transactional swap with rollback (fault-injected), checksum failure, and
unsafe-archive rejection.

Usage: python3 scripts/bridge_selftest.py
"""

import hashlib
import io
import itertools
import json
import os
import shutil
import sys
import tarfile
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "py"))
import briglia_bridge  # noqa: E402
import release_verify  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from signing_fixture import TestKey, manifest_bytes  # noqa: E402

CLI_KEY = APP_KEY = None            # created in main()
_SEQ = itertools.count(100)         # every fixture publish supersedes the last
LAST_SEQUENCE = {"cli": None, "app": None}


def cli_policy(cdn_dir, min_sequence=1):
    return release_verify.ReleasePolicy(
        "briglia-cli", CLI_KEY.keys(), "file://" + cdn_dir + "/manifest.sig.json",
        "file://" + cdn_dir + "/v{version}/", min_sequence)


def app_policy(cdn_dir, min_sequence=1):
    return release_verify.ReleasePolicy(
        "briglia-ut", APP_KEY.keys(), "file://" + cdn_dir + "/manifest.sig.json",
        "file://" + cdn_dir + "/v{version}/", min_sequence)


def publish_manifest(cdn_dir, version, digest, size, sequence=None,
                     asset="briglia-test.tar.gz"):
    """Sign + publish the channel manifest for one fixture release."""
    sequence = next(_SEQ) if sequence is None else sequence
    LAST_SEQUENCE["cli"] = sequence
    platforms = {"test-plat": {"url": "file://%s/v%s/%s" % (cdn_dir, version, asset),
                               "sha256": digest, "size": size}}
    with open(os.path.join(cdn_dir, "manifest.sig.json"), "wb") as f:
        f.write(CLI_KEY.sign(manifest_bytes("briglia-cli", version, sequence, platforms)))
    return sequence

PASSED = FAILED = 0


def check(label, ok, detail=""):
    global PASSED, FAILED
    print("%s %s%s" % ("PASS" if ok else "FAIL", label,
                       "" if ok or not detail else " — " + detail))
    if ok:
        PASSED += 1
    else:
        FAILED += 1


# The fake CLI speaks setup-api schema 2 (exact-match gate). When
# FAKE_API_DIR is set it records every setup-api call (argv + the stdin
# request) and serves canned responses from that directory, so the
# migrate/service round trips can be pinned without a real engine.
FAKE_BRIGLIA_OK = """#!/bin/sh
case "$1" in
  --version) echo "%(version)s";;
  bundle-check) exit 0;;
  setup-api)
    body="$(cat 2>/dev/null)"
    if [ -n "$FAKE_API_DIR" ]; then
      printf '%%s' "$body" > "$FAKE_API_DIR/stdin_$2"
      printf '%%s\\n' "$@" > "$FAKE_API_DIR/argv_$2"
      if [ -f "$FAKE_API_DIR/response_$2" ]; then cat "$FAKE_API_DIR/response_$2"; exit 0; fi
    fi
    case "$2" in
      status) echo '{"schema":2,"ok":true,"migration":{"needed":false,"conflict":false,"old_roots_present":[],"new_roots_present":[]}}';;
      *) echo '{"schema":2,"ok":true}';;
    esac;;
  *) exit 64;;
esac
"""

# The PREVIOUS identity's interface (schema 1): must be refused by the
# exact-schema gate — as a staged download and as an installed binary.
FAKE_LEGACY_SCHEMA1 = """#!/bin/sh
case "$1" in
  --version) echo "%(version)s";;
  bundle-check) exit 0;;
  setup-api) cat >/dev/null 2>&1 || true; echo '{"schema":1,"ok":true}';;
  *) exit 64;;
esac
"""

FAKE_FUTURE_SCHEMA3 = """#!/bin/sh
case "$1" in
  --version) echo "%(version)s";;
  bundle-check) exit 0;;
  setup-api) cat >/dev/null 2>&1 || true; echo '{"schema":3,"ok":true}';;
  *) exit 64;;
esac
"""

FAKE_BRIGLIA_PRE_SETUP_API = """#!/bin/sh
case "$1" in
  --version) echo "%(version)s";;
  bundle-check) exit 0;;
  *) echo "Error: Unknown subcommand" >&2; exit 64;;
esac
"""


def publish_release(cdn_dir, version, cli_script, bundle_marker="bundle-v1",
                    sequence=None, sha256=None):
    """Write the tarball for the test platform under cdn_dir/v<version>/
    and publish a SIGNED manifest for it (sha256 override = a signed lie)."""
    stage = tempfile.mkdtemp(prefix="briglia-ut-fake-release-")
    try:
        binary_path = os.path.join(stage, "briglia")
        with open(binary_path, "w") as f:
            f.write(cli_script % {"version": version})
        os.chmod(binary_path, 0o755)
        bundle = os.path.join(stage, briglia_bridge.BUNDLE_NAME)
        os.makedirs(bundle)
        with open(os.path.join(bundle, "marker.txt"), "w") as f:
            f.write(bundle_marker)
        os.makedirs(os.path.join(cdn_dir, "v" + version), exist_ok=True)
        tar_path = os.path.join(cdn_dir, "v" + version, "briglia-test.tar.gz")
        with tarfile.open(tar_path, "w:gz") as tar:
            tar.add(binary_path, arcname="briglia")
            tar.add(bundle, arcname=briglia_bridge.BUNDLE_NAME)
        digest = hashlib.sha256(open(tar_path, "rb").read()).hexdigest()
        publish_manifest(cdn_dir, version, sha256 or digest,
                         os.path.getsize(tar_path), sequence=sequence)
        return digest
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def read(path):
    with open(path) as f:
        return f.read()


def main():
    root = tempfile.mkdtemp(prefix="briglia-ut-bridge-selftest-")
    cdn = os.path.join(root, "cdn")
    install_dir = os.path.join(root, "bin")
    os.makedirs(cdn)
    os.makedirs(install_dir)

    # Point the bridge at the fixture world: a signed channel with
    # throwaway keys, and a private anti-rollback store.
    global CLI_KEY, APP_KEY
    CLI_KEY = TestKey("briglia-cli")
    APP_KEY = TestKey("briglia-ut")
    release_verify.TRUST_FILE = os.path.join(root, "trust", "release_trust.json")
    release_verify.CLI_POLICY = cli_policy(cdn)
    briglia_bridge.INSTALL_DIR = install_dir
    briglia_bridge.BRIGLIA = os.path.join(install_dir, "briglia")
    briglia_bridge._platform_key = lambda: "test-plat"
    briglia_bridge._wire_login_shell_path = lambda: None  # never touch real rc files
    bundle_dest = os.path.join(install_dir, briglia_bridge.BUNDLE_NAME)

    try:
        # 0. Attachment picker listing offers only directories and REGULAR
        # files: a FIFO (or device) would be a guaranteed daemon-side nack —
        # and, pre-hardening, a possible hang for anything that read it.
        pick = os.path.join(root, "pick")
        os.makedirs(pick)
        with open(os.path.join(pick, "note.txt"), "w") as f:
            f.write("hi")
        os.mkdir(os.path.join(pick, "sub"))
        os.mkfifo(os.path.join(pick, "pipe"))
        listing = briglia_bridge.list_dir(pick)
        names = sorted(e["name"] for e in listing.get("entries", []))
        check("list_dir hides non-regular files from the picker",
              listing.get("ok") is True and names == ["note.txt", "sub"],
              str(names))

        # 0b. Quick setup verifies the Telegram DESTINATION, not just the
        # token: getChat must succeed and be a private chat; the token
        # never leaks into the returned payload.
        calls = []
        def fake_api(token, method, params):
            calls.append((method, dict(params)))
            if token == "bad":
                return {"ok": False, "error_code": 401, "description": "Unauthorized"}
            cid = params.get("chat_id")
            if cid == "5551234567":
                return {"ok": True, "result": {"id": 5551234567, "type": "private",
                                               "first_name": "Sofia", "last_name": "Bruni",
                                               "username": "sofiab"}}
            if cid == "-100777":
                return {"ok": True, "result": {"id": -100777, "type": "supergroup", "title": "Team"}}
            return {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
        real_api = briglia_bridge._telegram_api
        briglia_bridge._telegram_api = fake_api
        try:
            r = briglia_bridge.telegram_get_chat("tok", "5551234567")
            check("getChat: private chat resolves to a human label",
                  r.get("ok") is True and r.get("label") == "Sofia Bruni (@sofiab)"
                  and calls[-1] == ("getChat", {"chat_id": "5551234567"}), str(r))
            r = briglia_bridge.telegram_get_chat("tok", "-100777")
            check("getChat: a group is refused (private chats only)",
                  r.get("ok") is False and "not a private chat" in r.get("error", ""), str(r))
            r = briglia_bridge.telegram_get_chat("tok", "42")
            check("getChat: unknown chat surfaces Telegram's description",
                  r.get("ok") is False and "chat not found" in r.get("error", ""), str(r))
            r = briglia_bridge.telegram_get_chat("bad", "5551234567")
            check("getChat: bad token surfaces Unauthorized", r.get("ok") is False
                  and "Unauthorized" in r.get("error", ""), str(r))
            r = briglia_bridge.telegram_get_chat("", "5551234567")
            check("getChat: empty inputs refused before any network call",
                  r.get("ok") is False and len(calls) == 4, str(r))
            def boom(token, method, params):
                raise OSError("no network")
            briglia_bridge._telegram_api = boom
            r = briglia_bridge.telegram_get_chat("tok", "5551234567")
            check("getChat: transport failure is an error, never a pass",
                  r.get("ok") is False and "could not reach Telegram" in r.get("error", ""), str(r))
            check("getChat: the token never appears in any payload",
                  all("tok" not in json.dumps(x) for x in [r]), str(r))
        finally:
            briglia_bridge._telegram_api = real_api

        # 1. Fresh install, happy path.
        publish_release(cdn, "9.9.9", FAKE_BRIGLIA_OK)
        result = briglia_bridge.install()
        check("fresh install succeeds", result["ok"] is True
              and result["version"] == "9.9.9", str(result))
        check("binary + bundle live, no leftovers",
              os.access(briglia_bridge.BRIGLIA, os.X_OK)
              and read(os.path.join(bundle_dest, "marker.txt")) == "bundle-v1"
              and not os.path.exists(briglia_bridge.BRIGLIA + ".new")
              and not os.path.exists(briglia_bridge.BRIGLIA + ".old")
              and not os.path.exists(bundle_dest + ".old")
              and not os.path.exists(os.path.join(install_dir, ".briglia-install-journal.json"))
              and not os.path.exists(os.path.join(install_dir, ".briglia-install-journal.json.tmp")))

        # 2. Update replaces both components.
        publish_release(cdn, "9.9.10", FAKE_BRIGLIA_OK, bundle_marker="bundle-v2")
        result = briglia_bridge.install()
        check("update succeeds", result["ok"] is True
              and result["version"] == "9.9.10", str(result))
        check("update replaced binary and bundle",
              "9.9.10" in read(briglia_bridge.BRIGLIA)
              and read(os.path.join(bundle_dest, "marker.txt")) == "bundle-v2")
        floor_after_update = LAST_SEQUENCE["cli"]
        check("signed channel: accepted sequence recorded as the floor",
              result.get("sequence") == floor_after_update
              and release_verify.trust_floor(
                  release_verify.CLI_POLICY.trust_domain)[0] == floor_after_update,
              str(result))

        # 2b. Signed channel: rollback and tampering are refused BEFORE any
        # download, and touch nothing.
        before_binary = read(briglia_bridge.BRIGLIA)
        publish_release(cdn, "9.9.10", FAKE_BRIGLIA_OK, bundle_marker="bundle-rb",
                        sequence=floor_after_update - 1)
        result = briglia_bridge.install()
        check("signed channel: older sequence than the floor refused (rollback)",
              result["ok"] is False and "rollback" in (result["error"] or "")
              and read(briglia_bridge.BRIGLIA) == before_binary
              and read(os.path.join(bundle_dest, "marker.txt")) == "bundle-v2", str(result))
        publish_release(cdn, "9.9.10", FAKE_BRIGLIA_OK, bundle_marker="bundle-tamper")
        env_path = os.path.join(cdn, "manifest.sig.json")
        envelope = json.load(open(env_path))
        envelope["signature"] = ("B" if envelope["signature"][0] != "B" else "C") + envelope["signature"][1:]
        json.dump(envelope, open(env_path, "w"))
        result = briglia_bridge.install()
        check("signed channel: tampered envelope refused (bad-signature)",
              result["ok"] is False and "bad-signature" in (result["error"] or "")
              and read(os.path.join(bundle_dest, "marker.txt")) == "bundle-v2", str(result))
        publish_release(cdn, "9.9.10", FAKE_BRIGLIA_OK, bundle_marker="bundle-min")
        release_verify.CLI_POLICY = cli_policy(cdn, min_sequence=LAST_SEQUENCE["cli"] + 1)
        result = briglia_bridge.install()
        check("signed channel: below the app's embedded minimum refused",
              result["ok"] is False and "rollback" in (result["error"] or ""), str(result))
        release_verify.CLI_POLICY = cli_policy(cdn)

        # 3. Incompatible (pre-setup-api) release: rejected BEFORE mutation.
        before_binary = read(briglia_bridge.BRIGLIA)
        publish_release(cdn, "0.1.42", FAKE_BRIGLIA_PRE_SETUP_API)
        result = briglia_bridge.install()
        check("pre-setup-api release refused with a clear reason",
              result["ok"] is False and "predates" in (result["error"] or ""),
              str(result))
        check("refusal touched nothing",
              read(briglia_bridge.BRIGLIA) == before_binary
              and read(os.path.join(bundle_dest, "marker.txt")) == "bundle-v2")
        # 3b. Exact-schema gate on the STAGED binary (rename plan §4.1): the
        # previous identity's schema 1 and an unknown newer schema are both
        # refused before mutation, each with the right advice.
        publish_release(cdn, "0.1.59", FAKE_LEGACY_SCHEMA1)
        result = briglia_bridge.install()
        check("staged schema-1 (old identity) release refused: 'update the CLI' wording",
              result["ok"] is False and "schema 1" in (result["error"] or "")
              and "NOT installed" in result["error"]
              and read(briglia_bridge.BRIGLIA) == before_binary, str(result))
        publish_release(cdn, "9.9.99", FAKE_FUTURE_SCHEMA3)
        result = briglia_bridge.install()
        check("staged schema-3 (newer than the app) release refused: 'update the app' wording",
              result["ok"] is False and "update the app" in (result["error"] or "")
              and read(briglia_bridge.BRIGLIA) == before_binary, str(result))

        # 4. Checksum mismatch: rejected, nothing touched.
        publish_release(cdn, "9.9.11", FAKE_BRIGLIA_OK, sha256="0" * 64)
        result = briglia_bridge.install()
        check("checksum mismatch refused",
              result["ok"] is False and "checksum" in (result["error"] or ""))
        check("checksum refusal touched nothing",
              read(briglia_bridge.BRIGLIA) == before_binary)

        # 5. Unsafe archive (path traversal member): rejected by the data
        # filter / guards, nothing touched.
        tar_path = os.path.join(cdn, "v9.9.11", "briglia-test.tar.gz")
        with tarfile.open(tar_path, "w:gz") as tar:
            info = tarfile.TarInfo("../evil.sh")
            payload = b"#!/bin/sh\n"
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        digest = hashlib.sha256(open(tar_path, "rb").read()).hexdigest()
        publish_manifest(cdn, "9.9.11", digest, os.path.getsize(tar_path))
        result = briglia_bridge.install()
        check("traversal member refused",
              result["ok"] is False and read(briglia_bridge.BRIGLIA) == before_binary,
              str(result))

        # 6. Fault injection mid-swap: bundle rename fails after the binary
        # already swapped — EVERYTHING must roll back (Codex's reproduction:
        # the old code left a new binary with a missing bundle).
        publish_release(cdn, "9.9.12", FAKE_BRIGLIA_OK, bundle_marker="bundle-v3")
        real_rename = os.replace

        def failing_rename(src, dst):
            if dst == bundle_dest and src.endswith(".new"):
                raise OSError("injected: disk exploded")
            real_rename(src, dst)

        briglia_bridge._rename = failing_rename
        result = briglia_bridge.install()
        briglia_bridge._rename = real_rename
        check("injected swap failure reported", result["ok"] is False
              and "swap failed" in (result["error"] or ""), str(result))
        check("old install fully restored after failed swap",
              read(briglia_bridge.BRIGLIA) == before_binary
              and read(os.path.join(bundle_dest, "marker.txt")) == "bundle-v2"
              and not os.path.exists(briglia_bridge.BRIGLIA + ".new")
              and not os.path.exists(briglia_bridge.BRIGLIA + ".old")
              and not os.path.exists(bundle_dest + ".new")
              and not os.path.exists(bundle_dest + ".old"))
        code, out, _ = briglia_bridge._run([briglia_bridge.BRIGLIA, "--version"])
        check("restored binary still runs", code == 0 and "9.9.10" in out)

        # 7. Recovery: the next install after a failure succeeds cleanly.
        result = briglia_bridge.install()
        check("install after failed swap succeeds", result["ok"] is True
              and result["version"] == "9.9.12", str(result))
        check("recovered to the new release",
              read(os.path.join(bundle_dest, "marker.txt")) == "bundle-v3")

        # 8. Crash-state matrix (Codex round 2): the exact on-disk states a
        # kill leaves after EACH swap rename. An install attempt with the
        # CDN unreachable must FIRST restore the parked components — before
        # any network access — and leave a working old installation even
        # though the install itself fails.
        old_binary = read(briglia_bridge.BRIGLIA)  # the 9.9.12-test script
        old_marker = "bundle-v3"
        newbin = "#!/bin/sh\necho half-swapped\n"

        def write_file(path, content):
            with open(path, "w") as f:
                f.write(content)
            os.chmod(path, 0o755)

        def make_bundle(path, marker):
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, "marker.txt"), "w") as f:
                f.write(marker)

        journal_path = os.path.join(briglia_bridge.INSTALL_DIR, ".briglia-install-journal.json")

        def write_journal(had_binary, had_bundle):
            with open(journal_path, "w") as f:
                json.dump({"had_binary": had_binary, "had_bundle": had_bundle,
                           "version": "crash-test"}, f)

        def reset_live():
            write_file(briglia_bridge.BRIGLIA, old_binary)
            shutil.rmtree(bundle_dest, ignore_errors=True)
            make_bundle(bundle_dest, old_marker)
            for p in (briglia_bridge.BRIGLIA + ".new", briglia_bridge.BRIGLIA + ".old", journal_path):
                if os.path.exists(p):
                    os.unlink(p)
            shutil.rmtree(bundle_dest + ".new", ignore_errors=True)
            shutil.rmtree(bundle_dest + ".old", ignore_errors=True)

        # The real flow writes the journal BEFORE the first rename, so
        # every reproducible crash state includes it (had_binary/had_bundle
        # true for these upgrade states).
        def s1():  # killed after: rename(ADA -> ADA.old)
            write_journal(True, True)
            os.rename(briglia_bridge.BRIGLIA, briglia_bridge.BRIGLIA + ".old")
            write_file(briglia_bridge.BRIGLIA + ".new", newbin)
            make_bundle(bundle_dest + ".new", "half")

        def s2():  # killed after: rename(ADA.new -> ADA)
            write_journal(True, True)
            os.rename(briglia_bridge.BRIGLIA, briglia_bridge.BRIGLIA + ".old")
            write_file(briglia_bridge.BRIGLIA, newbin)
            make_bundle(bundle_dest + ".new", "half")

        def s3():  # killed after: rename(bundle -> bundle.old)
            write_journal(True, True)
            os.rename(briglia_bridge.BRIGLIA, briglia_bridge.BRIGLIA + ".old")
            write_file(briglia_bridge.BRIGLIA, newbin)
            os.rename(bundle_dest, bundle_dest + ".old")
            make_bundle(bundle_dest + ".new", "half")

        def s4():  # killed after: rename(bundle.new -> bundle), before cleanup
            write_journal(True, True)
            os.rename(briglia_bridge.BRIGLIA, briglia_bridge.BRIGLIA + ".old")
            write_file(briglia_bridge.BRIGLIA, newbin)
            os.rename(bundle_dest, bundle_dest + ".old")
            make_bundle(bundle_dest, "half")

        good_policy = release_verify.CLI_POLICY
        for label, build_state in (("after binary parked", s1),
                                   ("after new binary live", s2),
                                   ("after bundle parked", s3),
                                   ("after full swap, backups parked", s4)):
            reset_live()
            build_state()
            release_verify.CLI_POLICY = cli_policy(os.path.join(root, "no-such-cdn"))
            result = briglia_bridge.install()
            restored = (result["ok"] is False
                        and read(briglia_bridge.BRIGLIA) == old_binary
                        and read(os.path.join(bundle_dest, "marker.txt")) == old_marker
                        and not os.path.exists(briglia_bridge.BRIGLIA + ".old")
                        and not os.path.exists(briglia_bridge.BRIGLIA + ".new")
                        and not os.path.exists(bundle_dest + ".old")
                        and not os.path.exists(bundle_dest + ".new")
                        and not os.path.exists(journal_path))
            check("crash recovery (%s): old install restored before network" % label,
                  restored, str(result))
        release_verify.CLI_POLICY = good_policy

        # 9. Crash state + reachable CDN: recover, then upgrade normally.
        reset_live()
        s3()
        publish_release(cdn, "9.9.13", FAKE_BRIGLIA_OK, bundle_marker="bundle-v4")
        result = briglia_bridge.install()
        check("crash state + good CDN: recovers then installs",
              result["ok"] is True and result["version"] == "9.9.13"
              and read(os.path.join(bundle_dest, "marker.txt")) == "bundle-v4"
              and not os.path.exists(journal_path),
              str(result))

        # 10. FRESH-install crash states (Codex round 3): no .old exists, so
        # recovery must rely on the journal's had_*=false and roll back to
        # "not installed" — never leave a binary without its bundle.
        def wipe_install():
            for p in (briglia_bridge.BRIGLIA, briglia_bridge.BRIGLIA + ".new",
                      briglia_bridge.BRIGLIA + ".old", journal_path):
                if os.path.exists(p):
                    os.unlink(p)
            for d in (bundle_dest, bundle_dest + ".new", bundle_dest + ".old"):
                shutil.rmtree(d, ignore_errors=True)

        def clean_after_fresh_crash():
            return (not os.path.exists(briglia_bridge.BRIGLIA)
                    and not os.path.isdir(bundle_dest)
                    and not os.path.exists(briglia_bridge.BRIGLIA + ".new")
                    and not os.path.isdir(bundle_dest + ".new")
                    and not os.path.exists(journal_path))

        # Codex's exact reproduction: killed after the new binary went live,
        # before the bundle did — with the CDN unreachable afterwards.
        wipe_install()
        write_journal(False, False)
        write_file(briglia_bridge.BRIGLIA, newbin)
        make_bundle(bundle_dest + ".new", "half")
        release_verify.CLI_POLICY = cli_policy(os.path.join(root, "no-such-cdn"))
        result = briglia_bridge.install()
        check("fresh crash (binary live, bundle staged): rolled back to not-installed",
              result["ok"] is False and clean_after_fresh_crash(), str(result))

        # Killed after both went live but before the journal delete.
        wipe_install()
        write_journal(False, False)
        write_file(briglia_bridge.BRIGLIA, newbin)
        make_bundle(bundle_dest, "half")
        result = briglia_bridge.install()
        check("fresh crash (both live, journal not cleared): rolled back to not-installed",
              result["ok"] is False and clean_after_fresh_crash(), str(result))

        # Staging-only crash (journal never written): just staging cleanup.
        wipe_install()
        write_file(briglia_bridge.BRIGLIA + ".new", newbin)
        make_bundle(bundle_dest + ".new", "half")
        result = briglia_bridge.install()
        check("fresh crash (staging only, no journal): cleaned to not-installed",
              result["ok"] is False and clean_after_fresh_crash(), str(result))
        release_verify.CLI_POLICY = good_policy

        # Fresh crash + reachable CDN: recovery, then a clean first install.
        wipe_install()
        write_journal(False, False)
        write_file(briglia_bridge.BRIGLIA, newbin)
        make_bundle(bundle_dest + ".new", "half")
        result = briglia_bridge.install()
        check("fresh crash + good CDN: recovers then installs",
              result["ok"] is True and result["version"] == "9.9.13"
              and read(os.path.join(bundle_dest, "marker.txt")) == "bundle-v4"
              and not os.path.exists(journal_path), str(result))

        # 11. No journal + stray .old = post-success garbage: the live
        # (validated) install stays, the .old is deleted, NOT restored —
        # partial restoration here is what created mixed-version installs.
        live_binary = read(briglia_bridge.BRIGLIA)
        write_file(briglia_bridge.BRIGLIA + ".old", "#!/bin/sh\necho stale\n")
        make_bundle(bundle_dest + ".old", "stale")
        publish_release(cdn, "9.9.14", FAKE_BRIGLIA_OK, bundle_marker="bundle-v5")
        result = briglia_bridge.install()
        check("stray .old without journal: dropped as garbage, install proceeds",
              result["ok"] is True and result["version"] == "9.9.14"
              and not os.path.exists(briglia_bridge.BRIGLIA + ".old")
              and not os.path.isdir(bundle_dest + ".old"), str(result))
        del live_binary

        # 12. Interrupting RECOVERY itself (Codex round 5): if the repairs
        # cannot be made durable, the journal must survive so the next run
        # retries — a lost journal over non-durable repairs would make a
        # later boot treat the half-swap as completed.
        real_fsync_dir = briglia_bridge._fsync_dir
        wipe_install()
        write_journal(False, False)
        write_file(briglia_bridge.BRIGLIA, newbin)
        make_bundle(bundle_dest + ".new", "half")
        briglia_bridge._fsync_dir = lambda path: "injected: I/O error"
        result = briglia_bridge.install()
        briglia_bridge._fsync_dir = real_fsync_dir
        check("recovery barrier failure: reported, journal retained for retry",
              result["ok"] is False
              and "durable" in (result["error"] or "")
              and os.path.exists(journal_path), str(result))
        result = briglia_bridge.install()
        check("recovery retry after barrier failure: repairs commit, install succeeds",
              result["ok"] is True and result["version"] == "9.9.14"
              and not os.path.exists(journal_path), str(result))

        # Idempotency: running recovery twice against the same upgrade
        # crash state must converge to the same clean restored state.
        old_binary2 = read(briglia_bridge.BRIGLIA)
        reset2_marker = "bundle-v5"
        for attempt in (1, 2):
            if attempt == 1:
                # construct S3-style state against the CURRENT install
                write_journal(True, True)
                os.rename(briglia_bridge.BRIGLIA, briglia_bridge.BRIGLIA + ".old")
                write_file(briglia_bridge.BRIGLIA, newbin)
                os.rename(bundle_dest, bundle_dest + ".old")
                make_bundle(bundle_dest + ".new", "half")
            release_verify.CLI_POLICY = cli_policy(os.path.join(root, "no-such-cdn"))
            result = briglia_bridge.install()
            check("recovery idempotent (run %d): restored state stable" % attempt,
                  result["ok"] is False
                  and read(briglia_bridge.BRIGLIA) == old_binary2
                  and read(os.path.join(bundle_dest, "marker.txt")) == reset2_marker
                  and not os.path.exists(journal_path)
                  and not os.path.exists(briglia_bridge.BRIGLIA + ".old")
                  and not os.path.isdir(bundle_dest + ".old"), str(result))
        release_verify.CLI_POLICY = good_policy

        # 13. Final commit sync failure (Codex round 6): if the post-unlink
        # directory sync fails, BOTH .old backups must survive and the
        # result must be an honest failure — deleting backups while the
        # journal's fate is uncertain opens a mixed-version reboot. The
        # injected wrapper fails only once the journal is gone, so the
        # earlier barriers run for real.
        publish_release(cdn, "9.9.15", FAKE_BRIGLIA_OK, bundle_marker="bundle-v6")
        pre_upgrade_binary = read(briglia_bridge.BRIGLIA)

        def fail_after_unlink(path):
            if not os.path.exists(journal_path):
                return "injected: commit sync I/O error"
            return real_fsync_dir(path)

        briglia_bridge._fsync_dir = fail_after_unlink
        result = briglia_bridge.install()
        briglia_bridge._fsync_dir = real_fsync_dir
        check("commit sync failure: honest error, BOTH backups retained, new install live",
              result["ok"] is False and "durable" in (result["error"] or "")
              and os.path.isfile(briglia_bridge.BRIGLIA + ".old")
              and os.path.isdir(bundle_dest + ".old")
              and read(briglia_bridge.BRIGLIA + ".old") == pre_upgrade_binary
              and "9.9.15" in read(briglia_bridge.BRIGLIA)
              and not os.path.exists(journal_path), str(result))
        # Reboot outcome B (journal absent): both backups discarded as
        # garbage, the validated new install stays. (Outcome A — journal
        # present restores BOTH old components — is section 8's s4 case.)
        result = briglia_bridge.install()
        check("retry after commit-sync failure: backups dropped, install settles",
              result["ok"] is True and result["version"] == "9.9.15"
              and not os.path.exists(briglia_bridge.BRIGLIA + ".old")
              and not os.path.isdir(bundle_dest + ".old"), str(result))

        # 14. M4 privileged/service helpers: fake sudo/systemctl/journalctl
        # on PATH. Pins the §2.5 security contract — the passcode travels
        # ONLY on sudo's stdin, never argv; privileged scripts are 0600
        # temp files deleted afterwards.
        fakebin = os.path.join(root, "fakebin")
        fakedir = os.path.join(root, "fakeout")
        os.makedirs(fakebin)
        os.makedirs(fakedir)

        def write_fake(name, body):
            path = os.path.join(fakebin, name)
            with open(path, "w") as f:
                f.write("#!/bin/sh\n" + body)
            os.chmod(path, 0o755)

        write_fake("sudo", """
printf '%s\n' "$@" > "$FAKE_DIR/sudo_args"
cat > "$FAKE_DIR/sudo_stdin"
if grep -q '^wrong$' "$FAKE_DIR/sudo_stdin"; then exit 1; fi
if [ "$4" = "sh" ] && [ "$5" != "-c" ] && [ -f "$5" ]; then
  cp "$5" "$FAKE_DIR/sudo_script_copy"
  printf '%s' "$5" > "$FAKE_DIR/sudo_script_path"
  python3 -c 'import os,sys; open(os.path.join(os.environ["FAKE_DIR"], "sudo_script_mode"), "w").write(oct(os.stat(sys.argv[1]).st_mode & 0o777))' "$5"
fi
echo done
""")
        write_fake("systemctl", 'printf \'%s\\n\' "$@" > "$FAKE_DIR/systemctl_args"\nexit 0\n')
        write_fake("journalctl", 'printf \'%s\\n\' "$@" > "$FAKE_DIR/journalctl_args"\necho "aug 27 log line one"\necho "aug 27 log line two"\n')

        old_path = os.environ.get("PATH", "")
        os.environ["FAKE_DIR"] = fakedir
        os.environ["PATH"] = fakebin + os.pathsep + old_path
        try:
            # run_sudo_command: strips "sudo ", passcode on stdin only.
            result = briglia_bridge.run_sudo_command(
                "sudo loginctl enable-linger phablet", "1234")
            sudo_args = read(os.path.join(fakedir, "sudo_args"))
            sudo_stdin = read(os.path.join(fakedir, "sudo_stdin"))
            check("run_sudo_command: ok + output", result["ok"] is True
                  and result["output"] == "done", str(result))
            check("run_sudo_command: -S/sh -c argv, sudo prefix stripped",
                  sudo_args.splitlines() == ["-S", "-p", "", "sh", "-c",
                                             "loginctl enable-linger phablet"],
                  sudo_args)
            check("run_sudo_command: passcode ONLY on stdin, never argv",
                  sudo_stdin == "1234\n" and "1234" not in sudo_args, sudo_args)

            result = briglia_bridge.run_sudo_command("", "1234")
            check("run_sudo_command: empty command refused without spawning",
                  result["ok"] is False and "empty" in result["error"], str(result))

            result = briglia_bridge.run_sudo_command("sudo true", "wrong")
            check("run_sudo_command: sudo exit 1 reported as passcode/refusal",
                  result["ok"] is False and "passcode" in result["error"], str(result))

            # run_privileged_script: 0600 temp file, deleted afterwards.
            for stale in ("sudo_script_copy", "sudo_script_mode", "sudo_script_path"):
                p = os.path.join(fakedir, stale)
                if os.path.exists(p):
                    os.unlink(p)
            script_text = "#!/bin/sh\necho wakelock-install\n"
            result = briglia_bridge.run_privileged_script(script_text, "1234")
            script_path = read(os.path.join(fakedir, "sudo_script_path"))
            check("run_privileged_script: ok, script content delivered verbatim",
                  result["ok"] is True
                  and read(os.path.join(fakedir, "sudo_script_copy")) == script_text,
                  str(result))
            check("run_privileged_script: script file was 0600 and is deleted after",
                  read(os.path.join(fakedir, "sudo_script_mode")) == "0o600"
                  and not os.path.exists(script_path)
                  and not os.path.isdir(os.path.dirname(script_path)),
                  script_path)
            check("run_privileged_script: passcode only on stdin",
                  read(os.path.join(fakedir, "sudo_stdin")) == "1234\n"
                  and "1234" not in read(os.path.join(fakedir, "sudo_args")), "")

            result = briglia_bridge.run_privileged_script("   ", "1234")
            check("run_privileged_script: empty script refused",
                  result["ok"] is False and "empty" in result["error"], str(result))

            # default_linger_command: local fallback for CLI releases that
            # don't serve service.linger_command in status (v0.1.43).
            linger = briglia_bridge.default_linger_command()
            import getpass as _getpass
            check("default_linger_command: sudo loginctl for the current user",
                  linger == "sudo loginctl enable-linger " + _getpass.getuser(),
                  linger)

            # systemctl_user: whitelist + argv shape.
            result = briglia_bridge.systemctl_user("start")
            check("systemctl_user start: ok + exact argv",
                  result["ok"] is True
                  and read(os.path.join(fakedir, "systemctl_args")).splitlines()
                      == ["--user", "start", "briglia.service"], str(result))
            os.unlink(os.path.join(fakedir, "systemctl_args"))
            result = briglia_bridge.systemctl_user("restart")
            check("systemctl_user: non-whitelisted action refused without spawning",
                  result["ok"] is False
                  and not os.path.exists(os.path.join(fakedir, "systemctl_args")),
                  str(result))

            # tail_journal: bounded count, unit-scoped argv.
            result = briglia_bridge.tail_journal(10)
            journal_args = read(os.path.join(fakedir, "journalctl_args")).splitlines()
            check("tail_journal: ok + lines",
                  result["ok"] is True and "log line two" in result["text"], str(result))
            check("tail_journal: --user, unit, -n argv",
                  journal_args[:5] == ["--user", "-u", "briglia.service", "-n", "10"],
                  str(journal_args))
            result = briglia_bridge.tail_journal("bogus")
            check("tail_journal: non-numeric count falls back to default",
                  result["ok"] is True and
                  read(os.path.join(fakedir, "journalctl_args")).splitlines()[4] == "40",
                  str(result))

            # Missing binaries (fakebin off PATH): honest errors, no crash.
            os.environ["PATH"] = os.path.join(root, "empty-path")
            result = briglia_bridge.tail_journal(5)
            check("tail_journal: journalctl missing → honest error",
                  result["ok"] is False and "not found" in result["error"], str(result))
            result = briglia_bridge.run_sudo_command("sudo true", "x")
            check("run_sudo_command: sudo missing → honest error",
                  result["ok"] is False and "not found" in result["error"], str(result))
        finally:
            os.environ["PATH"] = old_path
            os.environ.pop("FAKE_DIR", None)

        # 15. App self-update: settings, version compare, check/install chain.
        app_root = os.path.join(root, "appupd")
        app_cdn = os.path.join(app_root, "cdn")
        os.makedirs(app_cdn)
        settings_path = os.path.join(app_root, "cache", "app-settings.json")
        own_manifest = os.path.join(app_root, "own-manifest.json")
        with open(own_manifest, "w") as f:
            json.dump({"version": "0.5.0"}, f)
        os.environ["BRIGLIA_UT_APP_SETTINGS_PATH"] = settings_path
        os.environ["BRIGLIA_UT_APP_MANIFEST"] = own_manifest
        old_app_policy = release_verify.APP_POLICY
        release_verify.APP_POLICY = app_policy(app_cdn)

        def publish_app(version, data=b"click-bytes", sequence=None, **overrides):
            """Signed app-channel fixture; overrides are SIGNED lies about
            the click (size/sha256) or its asset name (filename)."""
            click_name = "briglia.permaevidence_%s_all.click" % version
            os.makedirs(os.path.join(app_cdn, "v" + version), exist_ok=True)
            with open(os.path.join(app_cdn, "v" + version, click_name), "wb") as f:
                f.write(data)
            entry = {"url": "file://%s/v%s/%s" % (app_cdn, version,
                                                  overrides.get("filename", click_name)),
                     "sha256": overrides.get("sha256", hashlib.sha256(data).hexdigest()),
                     "size": overrides.get("size", len(data))}
            sequence = next(_SEQ) if sequence is None else sequence
            LAST_SEQUENCE["app"] = sequence
            with open(os.path.join(app_cdn, "manifest.sig.json"), "wb") as f:
                f.write(APP_KEY.sign(manifest_bytes("briglia-ut", version, sequence,
                                                    {"click": entry})))

        pkcon_bin = os.path.join(app_root, "pkconbin")
        pkcon_out = os.path.join(app_root, "pkcon_args")
        os.makedirs(pkcon_bin)
        with open(os.path.join(pkcon_bin, "pkcon"), "w") as f:
            f.write('#!/bin/sh\nprintf \'%s\\n\' "$@" > "'
                    + pkcon_out + '"\nexit "${FAKE_PKCON_EXIT:-0}"\n')
        os.chmod(os.path.join(pkcon_bin, "pkcon"), 0o755)
        os.environ["PATH"] = pkcon_bin + os.pathsep + old_path
        # Legacy-image baseline: no com.lomiri.click tool, pkcon on PATH.
        # (Without this, a Homebrew gdbus on the dev Mac would be picked up.)
        os.environ["BRIGLIA_UT_CLICK_DBUS_TOOL"] = "none"
        try:
            # Settings: defaults, persistence, unknown-key refusal.
            check("app_settings: default auto_update off",
                  briglia_bridge.app_settings() == {"auto_update": False}, "")
            result = briglia_bridge.set_app_setting("auto_update", True)
            check("set_app_setting: persists and echoes",
                  result["ok"] is True
                  and result["settings"]["auto_update"] is True
                  and briglia_bridge.app_settings()["auto_update"] is True,
                  str(result))
            result = briglia_bridge.set_app_setting("bogus", True)
            check("set_app_setting: unknown key refused",
                  result["ok"] is False, str(result))
            with open(settings_path, "w") as f:
                f.write("{corrupt")
            check("app_settings: corrupt file → defaults",
                  briglia_bridge.app_settings() == {"auto_update": False}, "")
            briglia_bridge.set_app_setting("auto_update", False)

            # Version comparison: strictly-newer only, unparseable never.
            check("_version_newer: newer/equal/older/unparseable",
                  briglia_bridge._version_newer("0.5.1", "0.5.0") is True
                  and briglia_bridge._version_newer("0.5.0", "0.5.0") is False
                  and briglia_bridge._version_newer("0.4.9", "0.5.0") is False
                  and briglia_bridge._version_newer("v0.10.0", "0.9.9") is True
                  and briglia_bridge._version_newer("beta", "0.5.0") is False
                  and briglia_bridge._version_newer("0.6.0", "junk") is False, "")

            # Check: current, newer, malformed, suspicious filename.
            publish_app("0.5.0")
            result = briglia_bridge.app_update_check()
            check("app_update_check: same version → no update",
                  result["ok"] is True and result["update_available"] is False
                  and result["installed"] == "0.5.0", str(result))
            publish_app("0.6.0")
            result = briglia_bridge.app_update_check()
            check("app_update_check: newer version detected",
                  result["ok"] is True and result["update_available"] is True
                  and result["available"] == "0.6.0", str(result))
            publish_app("0.6.0", size="huge")
            result = briglia_bridge.app_update_check()
            check("app_update_check: signed-but-malformed manifest refused",
                  result["ok"] is False and "refused" in result["error"]
                  and result.get("kind") == "bad-platform", str(result))
            publish_app("0.6.0", filename="../evil.click")
            result = briglia_bridge.app_update_check()
            check("app_update_check: traversal filename refused",
                  result["ok"] is False and "filename" in result["error"],
                  str(result))

            # Install: happy path — pkcon called correctly, staging cleaned.
            publish_app("0.6.0")
            result = briglia_bridge.app_update_install()
            pkcon_args = read(pkcon_out).splitlines()
            staged = os.path.join(os.path.dirname(settings_path),
                                  "briglia.permaevidence_0.6.0_all.click")
            check("app_update_install: updates via pkcon install-local",
                  result["ok"] is True and result["updated"] is True
                  and result["needs_relaunch"] is True
                  and pkcon_args[:2] == ["install-local", "--allow-untrusted"]
                  and pkcon_args[2] == staged, str(result) + str(pkcon_args))
            check("app_update_install: staged click cleaned up",
                  not os.path.exists(staged), "")

            # Install: checksum mismatch → refused, pkcon NOT invoked.
            os.unlink(pkcon_out)
            publish_app("0.6.0", sha256="0" * 64)
            result = briglia_bridge.app_update_install()
            check("app_update_install: checksum mismatch refused, no pkcon",
                  result["ok"] is False and "checksum" in result["error"]
                  and not os.path.exists(pkcon_out), str(result))

            # Install: size mismatch → refused, no pkcon.
            publish_app("0.6.0", size=1)
            result = briglia_bridge.app_update_install()
            check("app_update_install: body beyond the signed size refused, no pkcon",
                  result["ok"] is False and "authenticated size" in result["error"]
                  and not os.path.exists(pkcon_out), str(result))

            # Signed channel: rollback + tampering refused, floor recorded
            # only after a VERIFIED install.
            publish_app("0.6.0", sequence=LAST_SEQUENCE["app"] - 5)
            result = briglia_bridge.app_update_install()
            check("app_update_install: older sequence than the floor refused (rollback)",
                  result["ok"] is False and result.get("kind") == "rollback"
                  and not os.path.exists(pkcon_out), str(result))
            publish_app("0.6.0")
            env_path = os.path.join(app_cdn, "manifest.sig.json")
            envelope = json.load(open(env_path))
            envelope["signature"] = ("B" if envelope["signature"][0] != "B" else "C") + envelope["signature"][1:]
            json.dump(envelope, open(env_path, "w"))
            result = briglia_bridge.app_update_install()
            check("app_update_install: tampered envelope refused, no pkcon",
                  result["ok"] is False and result.get("kind") == "bad-signature"
                  and not os.path.exists(pkcon_out), str(result))
            publish_app("0.6.0")
            result = briglia_bridge.app_update_install()
            check("app_update_install: verified install records the app floor",
                  result["ok"] is True and result["updated"] is True
                  and release_verify.trust_floor(
                      release_verify.APP_POLICY.trust_domain)[0] == LAST_SEQUENCE["app"],
                  str(result))
            os.unlink(pkcon_out)

            # Install: pkcon failure surfaces honestly, staging still cleaned.
            publish_app("0.6.0")
            os.environ["FAKE_PKCON_EXIT"] = "5"
            result = briglia_bridge.app_update_install()
            check("app_update_install: pkcon failure → honest error + cleanup",
                  result["ok"] is False
                  and "pkcon failed (exit 5)" in result["error"]
                  and not os.path.exists(staged), str(result))
            os.environ.pop("FAKE_PKCON_EXIT", None)

            # pkcon resolution: Lomiri apps get a slimmer PATH than the
            # Terminal (field bug: bare "pkcon" → 127), so a pkcon that is
            # NOT in PATH must still be found via absolute candidates.
            fake_pkcon = os.path.join(pkcon_bin, "pkcon")
            old_candidates = briglia_bridge.PKCON_CANDIDATES
            os.environ["PATH"] = "/nonexistent-path-entry"
            try:
                briglia_bridge.PKCON_CANDIDATES = (fake_pkcon,)
                publish_app("0.6.0")
                result = briglia_bridge.app_update_install()
                check("app_update_install: pkcon found via absolute "
                      "candidate when PATH lacks it",
                      result["ok"] is True and result["updated"] is True,
                      str(result))
                # Genuinely absent: 127 with a diagnostic (PATH + fallback
                # command) and the staged click still cleaned up.
                briglia_bridge.PKCON_CANDIDATES = (
                    os.path.join(app_root, "no-such-pkcon"),)
                publish_app("0.6.0")
                result = briglia_bridge.app_update_install()
                check("app_update_install: pkcon truly missing → "
                      "Morph/OpenStore advice + cleanup",
                      result["ok"] is False
                      and "pkcon is not installed" in result["error"]
                      and "Morph" in result["error"]
                      and "install-local" not in result["error"]
                      and not os.path.exists(staged), str(result))
            finally:
                briglia_bridge.PKCON_CANDIDATES = old_candidates
                os.environ["PATH"] = pkcon_bin + os.pathsep + old_path

            # Modern UT (24.04): pkcon does not exist AT ALL — installs
            # must go through the com.lomiri.click D-Bus service, exactly
            # what OpenStore itself calls (field bug, 2026-08-28).
            busctl_out = os.path.join(app_root, "busctl_args")
            fake_busctl = os.path.join(app_root, "busctl")
            with open(fake_busctl, "w") as f:
                f.write('#!/bin/sh\nprintf \'%s\\n\' "$@" > "'
                        + busctl_out + '"\nexit "${FAKE_BUSCTL_EXIT:-0}"\n')
            os.chmod(fake_busctl, 0o755)
            os.environ["BRIGLIA_UT_CLICK_DBUS_TOOL"] = fake_busctl
            os.environ["PATH"] = "/nonexistent-path-entry"
            old_cand2 = briglia_bridge.PKCON_CANDIDATES
            briglia_bridge.PKCON_CANDIDATES = (
                os.path.join(app_root, "no-such-pkcon"),)
            try:
                publish_app("0.6.0")
                result = briglia_bridge.app_update_install()
                dbus_args = read(busctl_out).splitlines()
                check("app_update_install: com.lomiri.click D-Bus install "
                      "works with NO pkcon on the image",
                      result["ok"] is True and result["updated"] is True
                      and dbus_args[:4] == ["call", "--system",
                                            "--timeout=300",
                                            "com.lomiri.click"]
                      and "Install" in dbus_args
                      and dbus_args[-1].endswith(".click"),
                      str(result) + str(dbus_args))

                # Both mechanisms dead → combined honest error with advice
                # that works on every device (Morph+OpenStore, NOT pkcon).
                os.environ["FAKE_BUSCTL_EXIT"] = "1"
                publish_app("0.6.0")
                result = briglia_bridge.app_update_install()
                check("app_update_install: D-Bus fail + no pkcon → both "
                      "attempts listed + Morph advice + cleanup",
                      result["ok"] is False
                      and "com.lomiri.click" in result["error"]
                      and "Morph" in result["error"]
                      and not os.path.exists(staged), str(result))

                # D-Bus fail on an image that still HAS pkcon → fallback.
                briglia_bridge.PKCON_CANDIDATES = (
                    os.path.join(pkcon_bin, "pkcon"),)
                publish_app("0.6.0")
                result = briglia_bridge.app_update_install()
                check("app_update_install: D-Bus fail → pkcon fallback",
                      result["ok"] is True and result["updated"] is True
                      and os.path.exists(pkcon_out), str(result))
                os.environ.pop("FAKE_BUSCTL_EXIT", None)

                # Registry truth check: installer claims success but the
                # click registry never reaches the target → honest error.
                registry = os.path.join(app_root, "registry")
                os.makedirs(os.path.join(registry, "current"))
                with open(os.path.join(registry, "current",
                                       "manifest.json"), "w") as f:
                    json.dump({"version": "0.5.0"}, f)
                os.environ["BRIGLIA_UT_CLICK_REGISTRY"] = registry
                os.environ["BRIGLIA_UT_CLICK_REGISTRY_WAIT"] = "0"
                publish_app("0.6.0")
                result = briglia_bridge.app_update_install()
                check("app_update_install: stale registry after 'success' "
                      "→ honest error",
                      result["ok"] is False
                      and "registry" in result["error"], str(result))
                with open(os.path.join(registry, "current",
                                       "manifest.json"), "w") as f:
                    json.dump({"version": "0.6.0"}, f)
                publish_app("0.6.0")
                result = briglia_bridge.app_update_install()
                check("app_update_install: registry reaches target → "
                      "verified ok",
                      result["ok"] is True and result["updated"] is True,
                      str(result))
            finally:
                briglia_bridge.PKCON_CANDIDATES = old_cand2
                os.environ["PATH"] = pkcon_bin + os.pathsep + old_path
                os.environ["BRIGLIA_UT_CLICK_DBUS_TOOL"] = "none"
                os.environ.pop("FAKE_BUSCTL_EXIT", None)
                os.environ.pop("BRIGLIA_UT_CLICK_REGISTRY", None)
                os.environ.pop("BRIGLIA_UT_CLICK_REGISTRY_WAIT", None)

            # Install: already current → updated False, no pkcon call.
            os.unlink(pkcon_out) if os.path.exists(pkcon_out) else None
            publish_app("0.5.0")
            result = briglia_bridge.app_update_install()
            check("app_update_install: already current → no-op",
                  result["ok"] is True and result["updated"] is False
                  and not os.path.exists(pkcon_out), str(result))

            # Auto-update: off → no run AND no network (dead CDN proves it).
            release_verify.APP_POLICY = app_policy(os.path.join(app_root, "no-such-cdn"))
            result = briglia_bridge.app_auto_update()
            check("app_auto_update: off → ran False, no fetch",
                  result == {"ran": False}, str(result))
            # Auto-update: on → runs the chain (dead CDN → honest error).
            briglia_bridge.set_app_setting("auto_update", True)
            result = briglia_bridge.app_auto_update()
            check("app_auto_update: on → ran True, surfaces fetch error",
                  result["ran"] is True and result["ok"] is False
                  and result.get("kind") == "unreachable", str(result))
            release_verify.APP_POLICY = app_policy(app_cdn)
            publish_app("0.6.0")
            result = briglia_bridge.app_auto_update()
            check("app_auto_update: on → installs newer version",
                  result["ran"] is True and result["ok"] is True
                  and result["updated"] is True, str(result))
        finally:
            os.environ["PATH"] = old_path
            os.environ.pop("FAKE_PKCON_EXIT", None)
            os.environ.pop("BRIGLIA_UT_CLICK_DBUS_TOOL", None)
            os.environ.pop("BRIGLIA_UT_APP_SETTINGS_PATH", None)
            os.environ.pop("BRIGLIA_UT_APP_MANIFEST", None)
            release_verify.APP_POLICY = old_app_policy

        # 16. Identity migration surface (rename plan §5). Detection is
        # read-only and points at a fixture home; the migration itself is
        # the CLI's setup-api `migrate` verb, pinned here through the fake
        # CLI's recorder; the keep-awake swap composes Briglia's served
        # install script + the legacy removal, new-before-old.
        print("— identity migration —")
        legacy_home = os.path.join(root, "legacy-home")
        os.makedirs(legacy_home)
        briglia_bridge.LEGACY_BINARY = os.path.join(legacy_home, ".local", "bin", "ada")
        briglia_bridge.LEGACY_CONFIG_ROOT = os.path.join(legacy_home, ".config", "ada")
        briglia_bridge.LEGACY_DATA_ROOT = os.path.join(legacy_home, ".local", "share", "ada")
        briglia_bridge.LEGACY_USER_UNIT = os.path.join(legacy_home, ".config", "systemd", "user", "ada.service")
        briglia_bridge.LEGACY_WAKELOCK_UNIT_PATH = os.path.join(legacy_home, "etc", "ada-keepawake.service")
        os.makedirs(os.path.dirname(briglia_bridge.LEGACY_BINARY))
        os.makedirs(os.path.dirname(briglia_bridge.LEGACY_USER_UNIT))
        os.makedirs(os.path.dirname(briglia_bridge.LEGACY_WAKELOCK_UNIT_PATH))

        ls = briglia_bridge.legacy_status()
        check("legacy_status: pristine home → nothing present",
              ls["present"] is False and ls["roots"] == [] and ls["binary"] is False
              and ls["compat_symlink"] is False and ls["wakelock_unit"] is False, str(ls))
        with open(briglia_bridge.LEGACY_WAKELOCK_UNIT_PATH, "w") as f:
            f.write("[Unit]\n")
        ls = briglia_bridge.legacy_status()
        check("legacy_status: a leftover unit file alone is NOT an install",
              ls["wakelock_unit"] is True and ls["present"] is False, str(ls))
        os.makedirs(briglia_bridge.LEGACY_CONFIG_ROOT)
        ls = briglia_bridge.legacy_status()
        check("legacy_status: old config root → present, listed",
              ls["present"] is True and ls["config_root"] is True
              and ls["roots"] == [briglia_bridge.LEGACY_CONFIG_ROOT], str(ls))
        os.symlink(briglia_bridge.BRIGLIA, briglia_bridge.LEGACY_BINARY)
        ls = briglia_bridge.legacy_status()
        check("legacy_status: post-migration compat symlink is NOT the old binary",
              ls["binary"] is False and ls["compat_symlink"] is True, str(ls))
        os.unlink(briglia_bridge.LEGACY_BINARY)
        with open(briglia_bridge.LEGACY_BINARY, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.makedirs(briglia_bridge.LEGACY_DATA_ROOT)
        with open(briglia_bridge.LEGACY_USER_UNIT, "w") as f:
            f.write("[Unit]\n")
        ls = briglia_bridge.legacy_status()
        check("legacy_status: real old binary + both roots + user unit",
              ls["binary"] is True and ls["compat_symlink"] is False
              and ls["roots"] == [briglia_bridge.LEGACY_CONFIG_ROOT, briglia_bridge.LEGACY_DATA_ROOT]
              and ls["user_unit"] is True and ls["present"] is True, str(ls))
        info = briglia_bridge.detect()
        check("detect: carries the legacy block and the briglia binary path",
              info["legacy"]["present"] is True and info["binary_path"] == briglia_bridge.BRIGLIA
              and info["installed"] is True, str({k: info[k] for k in ("installed", "binary_path")}))
        check("detect: legacy detection wrote nothing under the fixture home",
              sorted(os.listdir(legacy_home)) == [".config", ".local", "etc"], str(os.listdir(legacy_home)))

        # Installed-binary schema gate (an old-identity binary at the
        # Briglia path cannot happen on the signed channel, but the gate
        # must hold regardless of how the file got there).
        live_backup = read(briglia_bridge.BRIGLIA)
        with open(briglia_bridge.BRIGLIA, "w") as f:
            f.write(FAKE_LEGACY_SCHEMA1 % {"version": "0.1.59"})
        st = briglia_bridge.setup_api("status")
        check("setup_api: installed schema-1 binary → schema_mismatch, advises a CLI update",
              st.get("ok") is False and st["error"]["code"] == "schema_mismatch"
              and "update the CLI" in st["error"]["message"], str(st))
        with open(briglia_bridge.BRIGLIA, "w") as f:
            f.write(FAKE_FUTURE_SCHEMA3 % {"version": "9.9.99"})
        st = briglia_bridge.setup_api("status")
        check("setup_api: installed schema-3 binary → schema_mismatch, advises an app update",
              st.get("ok") is False and "update the app" in st["error"]["message"], str(st))
        with open(briglia_bridge.BRIGLIA, "w") as f:
            f.write(live_backup)

        # migrate(): the verb, the request shape, the passthrough.
        api_dir = os.path.join(root, "fake-api")
        os.makedirs(api_dir)
        os.environ["FAKE_API_DIR"] = api_dir
        try:
            captured = {}
            real_run = briglia_bridge._run

            def spying_run(argv, stdin_text=None, timeout=120):
                captured["timeout"] = timeout
                return real_run(argv, stdin_text=stdin_text, timeout=timeout)
            briglia_bridge._run = spying_run
            with open(os.path.join(api_dir, "response_migrate"), "w") as f:
                f.write(json.dumps({"schema": 2, "ok": True, "outcome": "migrated",
                                    "notes": ["compat symlink kept"], "log": ["step 1", "step 2"],
                                    "migration": {"needed": False}}))
            result = briglia_bridge.migrate()
            briglia_bridge._run = real_run
            check("migrate: calls `setup-api migrate` with an empty request object",
                  read(os.path.join(api_dir, "argv_migrate")).split() == ["setup-api", "migrate"]
                  and json.loads(read(os.path.join(api_dir, "stdin_migrate"))) == {},
                  read(os.path.join(api_dir, "argv_migrate")))
            check("migrate: response passed through verbatim (outcome, notes, log)",
                  result.get("ok") is True and result["outcome"] == "migrated"
                  and result["log"] == ["step 1", "step 2"] and result["notes"] == ["compat symlink kept"],
                  str(result))
            check("migrate: generous timeout (services stop/start + 60 s health probe)",
                  captured.get("timeout", 0) >= 600, str(captured))
            result = briglia_bridge.migrate(rollback=True)
            check("migrate(rollback): request carries rollback:true",
                  json.loads(read(os.path.join(api_dir, "stdin_migrate"))) == {"rollback": True},
                  read(os.path.join(api_dir, "stdin_migrate")))
            with open(os.path.join(api_dir, "response_migrate"), "w") as f:
                f.write(json.dumps({"schema": 2, "ok": False,
                                    "error": {"code": "migration_refused",
                                              "message": "new root already exists"},
                                    "log": ["capture"]}))
            result = briglia_bridge.migrate()
            check("migrate: the engine's typed refusal reaches the caller with its log",
                  result.get("ok") is False and result["error"]["code"] == "migration_refused"
                  and result["log"] == ["capture"], str(result))

            # Keep-awake swap composition.
            served_ok = ("#!/bin/sh\n# Briglia keep-awake unit installer\nset -e\n"
                         "mount -o remount,rw /\n"
                         "trap 'mount -o remount,ro / || echo busy' EXIT INT TERM HUP\n"
                         "cat > /etc/systemd/system/briglia-keepawake.service <<'BRIGLIA_UNIT'\n"
                         "[Unit]\nBRIGLIA_UNIT\nsystemctl daemon-reload\n"
                         "systemctl enable --now briglia-keepawake.service\n")

            def serve_service(**fields):
                payload = {"schema": 2, "ok": True}
                payload.update(fields)
                with open(os.path.join(api_dir, "response_service"), "w") as f:
                    f.write(json.dumps(payload))
                for stale in ("argv_service", "stdin_service"):
                    if os.path.exists(os.path.join(api_dir, stale)):
                        os.unlink(os.path.join(api_dir, stale))
            serve_service(wakelock_install_script=served_ok)
            composed = briglia_bridge.legacy_wakelock_swap_script()
            script = composed.get("script") or ""
            check("swap script: asks the CLI for its keep-awake scripts (keepawake_script:true)",
                  json.loads(read(os.path.join(api_dir, "stdin_service"))) == {"keepawake_script": True},
                  read(os.path.join(api_dir, "stdin_service")))
            check("swap script: composed = served install script, THEN legacy removal",
                  composed["ok"] is True and script.startswith(served_ok.rstrip("\n"))
                  and script.index("enable --now briglia-keepawake.service")
                      < script.index("disable --now ada-keepawake.service")
                  and ("rm -f " + briglia_bridge.LEGACY_WAKELOCK_UNIT_PATH) in script
                  and script.rstrip().endswith("systemctl daemon-reload"), script)
            check("swap script: both halves carry the read-only restore trap",
                  script.count(briglia_bridge.ROOT_SCRIPT_TRAP_MARKER) == 2, script)
            serve_service(wakelock_install_script=served_ok.replace("trap 'mount -o remount,ro / || echo busy' EXIT INT TERM HUP\n", ""))
            composed = briglia_bridge.legacy_wakelock_swap_script()
            check("swap script: served install script without the trap → refused",
                  composed["ok"] is False and "trap" in composed["error"], str(composed))
            serve_service(wakelock_install_script=served_ok.replace("briglia-keepawake", "ada-keepawake"))
            composed = briglia_bridge.legacy_wakelock_swap_script()
            check("swap script: served script naming the legacy unit → refused",
                  composed["ok"] is False and "legacy unit" in composed["error"], str(composed))
            serve_service()
            composed = briglia_bridge.legacy_wakelock_swap_script()
            check("swap script: CLI served no install script → refused",
                  composed["ok"] is False and "did not serve" in composed["error"], str(composed))
            with open(os.path.join(api_dir, "response_service"), "w") as f:
                f.write(json.dumps({"schema": 2, "ok": False,
                                    "error": {"code": "migration_needed",
                                              "message": "run briglia migrate first"}}))
            composed = briglia_bridge.legacy_wakelock_swap_script()
            check("swap script: setup-api refusal (e.g. migration still pending) passed through",
                  composed["ok"] is False and "run briglia migrate first" in composed["error"], str(composed))
            os.unlink(briglia_bridge.LEGACY_WAKELOCK_UNIT_PATH)
            for stale in ("argv_service", "stdin_service"):
                if os.path.exists(os.path.join(api_dir, stale)):
                    os.unlink(os.path.join(api_dir, stale))
            composed = briglia_bridge.legacy_wakelock_swap_script()
            check("swap script: no legacy unit → 'nothing to swap', CLI not even asked",
                  composed["ok"] is False and "nothing to swap" in composed["error"]
                  and not os.path.exists(os.path.join(api_dir, "argv_service")), str(composed))

            # swap_legacy_wakelock(): runs under the fake sudo; the verdict
            # is truthful about the unit file, not about sudo's exit code.
            with open(briglia_bridge.LEGACY_WAKELOCK_UNIT_PATH, "w") as f:
                f.write("[Unit]\n")
            serve_service(wakelock_install_script=served_ok)
            os.environ["FAKE_DIR"] = fakedir
            os.environ["PATH"] = fakebin + os.pathsep + old_path
            try:
                for stale in ("sudo_args", "sudo_stdin", "sudo_script_copy"):
                    if os.path.exists(os.path.join(fakedir, stale)):
                        os.unlink(os.path.join(fakedir, stale))
                result = briglia_bridge.swap_legacy_wakelock("1234")
                check("swap: sudo ran the composed script, passcode only on stdin",
                      os.path.exists(os.path.join(fakedir, "sudo_script_copy"))
                      and "disable --now ada-keepawake.service" in read(os.path.join(fakedir, "sudo_script_copy"))
                      and read(os.path.join(fakedir, "sudo_stdin")) == "1234\n"
                      and "1234" not in read(os.path.join(fakedir, "sudo_args")), str(result))
                check("swap: exit 0 but the legacy unit file survived → reported as FAILURE",
                      result["ok"] is False and result["legacy_unit_remaining"] is True
                      and "still" in result["error"], str(result))
                # A sudo that really removes the unit (what the script does as root).
                write_fake("sudo", 'cat > /dev/null\nrm -f "$FAKE_LEGACY_UNIT"\necho done\n')
                os.environ["FAKE_LEGACY_UNIT"] = briglia_bridge.LEGACY_WAKELOCK_UNIT_PATH
                result = briglia_bridge.swap_legacy_wakelock("1234")
                check("swap: legacy unit gone after the script → success",
                      result["ok"] is True and result["legacy_unit_remaining"] is False, str(result))
                os.unlink(os.path.join(fakedir, "sudo_script_copy"))
                result = briglia_bridge.swap_legacy_wakelock("1234")
                check("swap: nothing to swap → sudo never spawned",
                      result["ok"] is False and "nothing to swap" in result["error"]
                      and not os.path.exists(os.path.join(fakedir, "sudo_script_copy")), str(result))
            finally:
                os.environ["PATH"] = old_path
                os.environ.pop("FAKE_DIR", None)
                os.environ.pop("FAKE_LEGACY_UNIT", None)
        finally:
            os.environ.pop("FAKE_API_DIR", None)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\nbridge selftest: %d passed, %d failed" % (PASSED, FAILED))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
