#!/usr/bin/env python3
"""Battery for scripts/release/rename-keys-dir.sh — the Stage-8 cutover helper
that moves the publishing Mac's release-key directory to the new identity
(rename plan §3.3) — run against a throwaway HOME. Proves: dry-run changes
nothing; the rename is a single directory move plus byte-identical twins with
rewritten IDs; old files, backups and unrelated files are untouched; the run
is idempotent and resumable; half-done / absent / watcher-loaded states are
refused with nothing changed.

    python3 scripts/keys_rename_selftest.py
"""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "release", "rename-keys-dir.sh")

PASSED = FAILED = 0


def check(label, ok, detail=""):
    global PASSED, FAILED
    print("  %s %s%s" % ("✔" if ok else "✖", label, "" if ok or not detail else " — " + str(detail)[-600:]))
    if ok:
        PASSED += 1
    else:
        FAILED += 1


def tree(root):
    """{relpath: (mode, sha256)} of every file under root, for change detection."""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(dirpath, name)
            with open(p, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
            out[os.path.relpath(p, root)] = (stat.S_IMODE(os.lstat(p).st_mode), digest)
    return out


CLI_OLD = "ada-cli-release-v1-94d967bae0867c2e"
UT_OLD = "ada-ut-release-v1-7bb0163ac16c5cb3"
CLI_NEW = "briglia-cli-release-v1-94d967bae0867c2e"
UT_NEW = "briglia-ut-release-v1-7bb0163ac16c5cb3"


def seed(home):
    old = os.path.join(home, ".ada-release-keys")
    os.makedirs(os.path.join(old, "backup"), mode=0o700)
    docs = os.path.join(home, "Documents", "Ada-Release-Key-Backup")
    os.makedirs(docs, mode=0o700)

    def rec(key_id, channel):
        return json.dumps({"keyId": key_id, "channel": channel, "publicKeyHex": "ab" * 32,
                           "fingerprintSHA256": key_id.rsplit("-", 1)[1] + "00" * 24,
                           "created": "2026-08-31T06:40:12.527213+00:00"}, indent=2) + "\n"

    def put(path, content, mode=0o600):
        with open(path, "w") as f:
            f.write(content)
        os.chmod(path, mode)

    put(os.path.join(old, CLI_OLD + ".json"), rec(CLI_OLD, "ada-cli"))
    put(os.path.join(old, CLI_OLD + ".pub.pem"), "-----BEGIN PUBLIC KEY-----\ncli\n-----END PUBLIC KEY-----\n")
    put(os.path.join(old, UT_OLD + ".json"), rec(UT_OLD, "ada-ut"))
    put(os.path.join(old, UT_OLD + ".pub.pem"), "-----BEGIN PUBLIC KEY-----\nut\n-----END PUBLIC KEY-----\n")
    put(os.path.join(old, UT_OLD + ".priv.pem"), "-----BEGIN PRIVATE KEY-----\nSECRET\n-----END PRIVATE KEY-----\n")
    put(os.path.join(old, "ada-ut-publications.jsonl"), '{"tag": "v0.7.4", "sequence": 1}\n', 0o644)
    put(os.path.join(old, "ada-ut-publish.lock"), "")
    for d in (os.path.join(old, "backup"), docs):
        put(os.path.join(d, "README.md"), "# backups\n")
        put(os.path.join(d, CLI_OLD + ".priv.pem.enc"), "ENCRYPTED-CLI")
        put(os.path.join(d, UT_OLD + ".priv.pem.enc"), "ENCRYPTED-UT")
        put(os.path.join(d, CLI_OLD + ".json"), rec(CLI_OLD, "ada-cli"))
        put(os.path.join(d, UT_OLD + ".json"), rec(UT_OLD, "ada-ut"))
    return old, docs


_SHIMS = {}


def launchctl_shim(loaded):
    """A PATH dir whose `launchctl print` reports the previous watcher agent as
    loaded (exit 0) or not (exit 1) — the real one on the publishing Mac must
    never decide a test."""
    key = "on" if loaded else "off"
    if key not in _SHIMS:
        d = tempfile.mkdtemp(prefix="briglia-keys-shim-%s-" % key)
        with open(os.path.join(d, "launchctl"), "w") as f:
            f.write("#!/bin/bash\nexit %d\n" % (0 if loaded else 1))
        os.chmod(os.path.join(d, "launchctl"), 0o755)
        _SHIMS[key] = d
    return _SHIMS[key]


def run(home, *args, extra_env=None, watcher_loaded=False):
    env = dict(os.environ, BRIGLIA_KEYS_HOME=home,
               PATH=launchctl_shim(watcher_loaded) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"))
    if extra_env:
        env.update(extra_env)
    p = subprocess.run(["bash", SCRIPT, *args], capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


def main():
    root = tempfile.mkdtemp(prefix="briglia-keys-rename-selftest-")
    try:
        # ---------------------------------------------------------- dry run
        print("— dry-run —")
        home = os.path.join(root, "h1"); os.makedirs(home)
        old, docs = seed(home)
        before = tree(home)
        rc, out = run(home, "--dry-run")
        check("dry-run exits 0 and names every planned action", rc == 0 and "(dry-run) rename" in out
              and CLI_NEW + ".json" in out and UT_NEW + ".priv.pem" in out and "README-RENAME.txt" in out, out)
        check("dry-run changes nothing (every file, mode and byte)", tree(home) == before and os.path.isdir(old))
        rc, out = run(home, "--bogus")
        check("unknown option → usage, exit 64, nothing changed", rc == 64 and tree(home) == before, out)

        # ---------------------------------------------------------- the rename
        print("— rename —")
        rc, out = run(home)
        new = os.path.join(home, ".briglia-release-keys")
        check("exit 0, old directory gone, new directory present", rc == 0 and not os.path.exists(old) and os.path.isdir(new), out)
        after = tree(home)
        moved = {k.replace(".ada-release-keys", ".briglia-release-keys", 1): v for k, v in before.items()}
        check("every previous file survived the move byte-for-byte with its mode (old-named files kept)",
              all(after.get(k) == v for k, v in moved.items()),
              [k for k, v in moved.items() if after.get(k) != v])
        for old_id, new_id, chan in ((CLI_OLD, CLI_NEW, "briglia-cli"), (UT_OLD, UT_NEW, "briglia-ut")):
            rec_new = json.load(open(os.path.join(new, new_id + ".json")))
            rec_old = json.load(open(os.path.join(new, old_id + ".json")))
            check("%s.json: keyId/channel rewritten, every other field verbatim, mode 0600" % new_id,
                  rec_new["keyId"] == new_id and rec_new["channel"] == chan
                  and {k: v for k, v in rec_new.items() if k not in ("keyId", "channel")}
                  == {k: v for k, v in rec_old.items() if k not in ("keyId", "channel")}
                  and after[os.path.relpath(os.path.join(new, new_id + ".json"), home)][0] == 0o600, rec_new)
            check("%s.pub.pem is a byte copy" % new_id,
                  after[os.path.relpath(os.path.join(new, new_id + ".pub.pem"), home)][1]
                  == after[os.path.relpath(os.path.join(new, old_id + ".pub.pem"), home)][1])
        priv_rel = os.path.relpath(os.path.join(new, UT_NEW + ".priv.pem"), home)
        check("app private key twin: byte copy, mode 0600",
              after[priv_rel] == after[os.path.relpath(os.path.join(new, UT_OLD + ".priv.pem"), home)] and after[priv_rel][0] == 0o600)
        check("no private-key twin invented for the CLI key (it has no local private key)",
              not os.path.exists(os.path.join(new, CLI_NEW + ".priv.pem")))
        check("publication log and lock untouched under the new directory, no new publication log created",
              after[os.path.relpath(os.path.join(new, "ada-ut-publications.jsonl"), home)] == before[".ada-release-keys/ada-ut-publications.jsonl"]
              and not os.path.exists(os.path.join(new, "briglia-ut-publications.jsonl")))
        for d, label in ((os.path.join(new, "backup"), "key-dir backup/"), (docs, "~/Documents backup")):
            note = os.path.join(d, "README-RENAME.txt")
            text = open(note).read() if os.path.exists(note) else ""
            check("%s: README-RENAME.txt written (0600) naming both id transitions, backups themselves untouched" % label,
                  os.path.exists(note) and stat.S_IMODE(os.stat(note).st_mode) == 0o600
                  and CLI_OLD + "  →  " + CLI_NEW in text and UT_OLD + "  →  " + UT_NEW in text
                  and "did NOT change" in text
                  and open(os.path.join(d, CLI_OLD + ".priv.pem.enc")).read() == "ENCRYPTED-CLI"
                  and open(os.path.join(d, UT_OLD + ".priv.pem.enc")).read() == "ENCRYPTED-UT"
                  and not os.path.exists(os.path.join(d, CLI_NEW + ".priv.pem.enc")), text)
        extra = sorted(set(after) - set(moved))
        expected_extra = sorted([
            os.path.join(".briglia-release-keys", CLI_NEW + ".json"),
            os.path.join(".briglia-release-keys", CLI_NEW + ".pub.pem"),
            os.path.join(".briglia-release-keys", UT_NEW + ".json"),
            os.path.join(".briglia-release-keys", UT_NEW + ".pub.pem"),
            os.path.join(".briglia-release-keys", UT_NEW + ".priv.pem"),
            os.path.join(".briglia-release-keys", "backup", "README-RENAME.txt"),
            os.path.join("Documents", "Ada-Release-Key-Backup", "README-RENAME.txt"),
        ])
        check("exactly the expected new files and nothing else", extra == expected_extra, extra)

        # ---------------------------------------------------------- idempotence
        print("— idempotence / resume / refusals —")
        rc, out = run(home)
        check("second run: 'already renamed', exit 0, nothing changed", rc == 0 and "already renamed" in out and tree(home) == after, out)
        os.unlink(os.path.join(new, UT_NEW + ".priv.pem"))
        os.unlink(os.path.join(new, CLI_NEW + ".json"))
        rc, out = run(home)
        check("resume after a crash that lost twins: completes only the missing twins, byte-identical result",
              rc == 0 and "completed the missing twins/notes (2)" in out and tree(home) == after, out)
        with open(os.path.join(new, CLI_NEW + ".json"), "a") as f:
            f.write("tampered\n")
        os.unlink(os.path.join(new, UT_NEW + ".pub.pem"))
        rc, out = run(home)
        check("a twin with DIFFERENT content is never overwritten: refusal, exit 1",
              rc == 1 and "exists with different content" in out and open(os.path.join(new, CLI_NEW + ".json")).read().endswith("tampered\n"), out)

        home2 = os.path.join(root, "h2"); os.makedirs(home2)
        old2, _ = seed(home2)
        os.makedirs(os.path.join(home2, ".briglia-release-keys"))
        before2 = tree(home2)
        rc, out = run(home2)
        check("both directories present → refusal, exit 1, nothing changed", rc == 1 and "both" in out and tree(home2) == before2, out)

        home3 = os.path.join(root, "h3"); os.makedirs(home3)
        rc, out = run(home3)
        check("neither directory present → refusal, exit 1", rc == 1 and "nothing to rename" in out, out)

        home4 = os.path.join(root, "h4"); os.makedirs(home4)
        old4, _ = seed(home4)
        os.unlink(os.path.join(old4, CLI_OLD + ".json")); os.unlink(os.path.join(old4, UT_OLD + ".json"))
        before4 = tree(home4)
        rc, out = run(home4)
        check("no key records in the old directory (wrong directory) → refusal, nothing changed",
              rc == 1 and "no ada-*-release-v1-*.json" in out and tree(home4) == before4, out)

        home5 = os.path.join(root, "h5"); os.makedirs(home5)
        old5, _ = seed(home5)
        with open(os.path.join(old5, "ada-cli-release-v1-0000000000000000.json"), "w") as f:
            f.write(json.dumps({"keyId": "foreign-id", "channel": "ada-cli"}))
        before5 = tree(home5)
        rc, out = run(home5)
        check("a key record whose keyId is not a previous-identity id → refusal after the directory move only "
              "(re-runnable: nothing else written)",
              rc == 1 and "not a previous-identity id" in out
              and not os.path.exists(os.path.join(home5, ".briglia-release-keys", CLI_NEW + ".json"))
              and {k.replace(".briglia-release-keys", ".ada-release-keys", 1): v for k, v in tree(home5).items()} == before5, out)

        if sys.platform == "darwin":
            home6 = os.path.join(root, "h6"); os.makedirs(home6)
            seed(home6)
            before6 = tree(home6)
            rc, out = run(home6, watcher_loaded=True)
            check("previous watcher agent still loaded (launchctl shim) → refusal, exit 1, nothing changed",
                  rc == 1 and "still loaded" in out and tree(home6) == before6, out)
        else:
            print("  (launchctl refusal check skipped: macOS only)")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        for d in _SHIMS.values():
            shutil.rmtree(d, ignore_errors=True)

    print("\nkeys-rename selftest: %d passed, %d failed" % (PASSED, FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
