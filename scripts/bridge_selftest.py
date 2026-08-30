#!/usr/bin/env python3
"""Offline selftest for py/ada_bridge.py's install pipeline.

Runs on any POSIX dev box (no device, no network): serves a fake release
from a file:// BASE_URL, with a fake `ada` shell script that answers
--version, bundle-check and setup-api status. Covers the Codex findings of
2026-08-27: staged setup-api/schema validation BEFORE mutation, the
transactional swap with rollback (fault-injected), checksum failure, and
unsafe-archive rejection.

Usage: python3 scripts/bridge_selftest.py
"""

import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "py"))
import ada_bridge  # noqa: E402

PASSED = FAILED = 0


def check(label, ok, detail=""):
    global PASSED, FAILED
    print("%s %s%s" % ("PASS" if ok else "FAIL", label,
                       "" if ok or not detail else " — " + detail))
    if ok:
        PASSED += 1
    else:
        FAILED += 1


FAKE_ADA_OK = """#!/bin/sh
case "$1" in
  --version) echo "%(version)s";;
  bundle-check) exit 0;;
  setup-api) cat >/dev/null 2>&1 || true; echo '{"schema":1,"ok":true}';;
  *) exit 64;;
esac
"""

FAKE_ADA_PRE_SETUP_API = """#!/bin/sh
case "$1" in
  --version) echo "%(version)s";;
  bundle-check) exit 0;;
  *) echo "Error: Unknown subcommand" >&2; exit 64;;
esac
"""


def publish_release(cdn_dir, version, ada_script, bundle_marker="bundle-v1"):
    """Write manifest + tarball for the test platform into cdn_dir."""
    stage = tempfile.mkdtemp(prefix="ada-ut-fake-release-")
    try:
        ada_path = os.path.join(stage, "ada")
        with open(ada_path, "w") as f:
            f.write(ada_script % {"version": version})
        os.chmod(ada_path, 0o755)
        bundle = os.path.join(stage, ada_bridge.BUNDLE_NAME)
        os.makedirs(bundle)
        with open(os.path.join(bundle, "marker.txt"), "w") as f:
            f.write(bundle_marker)
        tar_path = os.path.join(cdn_dir, "ada-test.tar.gz")
        with tarfile.open(tar_path, "w:gz") as tar:
            tar.add(ada_path, arcname="ada")
            tar.add(bundle, arcname=ada_bridge.BUNDLE_NAME)
        digest = hashlib.sha256(open(tar_path, "rb").read()).hexdigest()
        manifest = {"version": version, "platforms": {"test-plat": {
            "url": "file://" + tar_path, "sha256": digest}}}
        with open(os.path.join(cdn_dir, "manifest.json"), "w") as f:
            json.dump(manifest, f)
        return digest
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def read(path):
    with open(path) as f:
        return f.read()


def main():
    root = tempfile.mkdtemp(prefix="ada-ut-bridge-selftest-")
    cdn = os.path.join(root, "cdn")
    install_dir = os.path.join(root, "bin")
    os.makedirs(cdn)
    os.makedirs(install_dir)

    # Point the bridge at the fixture world.
    ada_bridge.BASE_URL = "file://" + cdn
    ada_bridge.INSTALL_DIR = install_dir
    ada_bridge.ADA = os.path.join(install_dir, "ada")
    ada_bridge._platform_key = lambda: "test-plat"
    ada_bridge._wire_login_shell_path = lambda: None  # never touch real rc files
    bundle_dest = os.path.join(install_dir, ada_bridge.BUNDLE_NAME)

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
        listing = ada_bridge.list_dir(pick)
        names = sorted(e["name"] for e in listing.get("entries", []))
        check("list_dir hides non-regular files from the picker",
              listing.get("ok") is True and names == ["note.txt", "sub"],
              str(names))

        # 1. Fresh install, happy path.
        publish_release(cdn, "9.9.9-test", FAKE_ADA_OK)
        result = ada_bridge.install()
        check("fresh install succeeds", result["ok"] is True
              and result["version"] == "9.9.9-test", str(result))
        check("binary + bundle live, no leftovers",
              os.access(ada_bridge.ADA, os.X_OK)
              and read(os.path.join(bundle_dest, "marker.txt")) == "bundle-v1"
              and not os.path.exists(ada_bridge.ADA + ".new")
              and not os.path.exists(ada_bridge.ADA + ".old")
              and not os.path.exists(bundle_dest + ".old")
              and not os.path.exists(os.path.join(install_dir, ".ada-install-journal.json"))
              and not os.path.exists(os.path.join(install_dir, ".ada-install-journal.json.tmp")))

        # 2. Update replaces both components.
        publish_release(cdn, "9.9.10-test", FAKE_ADA_OK, bundle_marker="bundle-v2")
        result = ada_bridge.install()
        check("update succeeds", result["ok"] is True
              and result["version"] == "9.9.10-test", str(result))
        check("update replaced binary and bundle",
              "9.9.10-test" in read(ada_bridge.ADA)
              and read(os.path.join(bundle_dest, "marker.txt")) == "bundle-v2")

        # 3. Incompatible (pre-setup-api) release: rejected BEFORE mutation.
        before_binary = read(ada_bridge.ADA)
        publish_release(cdn, "0.1.42-old", FAKE_ADA_PRE_SETUP_API)
        result = ada_bridge.install()
        check("pre-setup-api release refused with a clear reason",
              result["ok"] is False and "predates" in (result["error"] or ""),
              str(result))
        check("refusal touched nothing",
              read(ada_bridge.ADA) == before_binary
              and read(os.path.join(bundle_dest, "marker.txt")) == "bundle-v2")

        # 4. Checksum mismatch: rejected, nothing touched.
        publish_release(cdn, "9.9.11-test", FAKE_ADA_OK)
        manifest = json.load(open(os.path.join(cdn, "manifest.json")))
        manifest["platforms"]["test-plat"]["sha256"] = "0" * 64
        json.dump(manifest, open(os.path.join(cdn, "manifest.json"), "w"))
        result = ada_bridge.install()
        check("checksum mismatch refused",
              result["ok"] is False and "checksum" in (result["error"] or ""))
        check("checksum refusal touched nothing",
              read(ada_bridge.ADA) == before_binary)

        # 5. Unsafe archive (path traversal member): rejected by the data
        # filter / guards, nothing touched.
        tar_path = os.path.join(cdn, "ada-test.tar.gz")
        with tarfile.open(tar_path, "w:gz") as tar:
            info = tarfile.TarInfo("../evil.sh")
            payload = b"#!/bin/sh\n"
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        digest = hashlib.sha256(open(tar_path, "rb").read()).hexdigest()
        manifest["platforms"]["test-plat"]["sha256"] = digest
        json.dump(manifest, open(os.path.join(cdn, "manifest.json"), "w"))
        result = ada_bridge.install()
        check("traversal member refused",
              result["ok"] is False and read(ada_bridge.ADA) == before_binary,
              str(result))

        # 6. Fault injection mid-swap: bundle rename fails after the binary
        # already swapped — EVERYTHING must roll back (Codex's reproduction:
        # the old code left a new binary with a missing bundle).
        publish_release(cdn, "9.9.12-test", FAKE_ADA_OK, bundle_marker="bundle-v3")
        real_rename = os.replace

        def failing_rename(src, dst):
            if dst == bundle_dest and src.endswith(".new"):
                raise OSError("injected: disk exploded")
            real_rename(src, dst)

        ada_bridge._rename = failing_rename
        result = ada_bridge.install()
        ada_bridge._rename = real_rename
        check("injected swap failure reported", result["ok"] is False
              and "swap failed" in (result["error"] or ""), str(result))
        check("old install fully restored after failed swap",
              read(ada_bridge.ADA) == before_binary
              and read(os.path.join(bundle_dest, "marker.txt")) == "bundle-v2"
              and not os.path.exists(ada_bridge.ADA + ".new")
              and not os.path.exists(ada_bridge.ADA + ".old")
              and not os.path.exists(bundle_dest + ".new")
              and not os.path.exists(bundle_dest + ".old"))
        code, out, _ = ada_bridge._run([ada_bridge.ADA, "--version"])
        check("restored binary still runs", code == 0 and "9.9.10-test" in out)

        # 7. Recovery: the next install after a failure succeeds cleanly.
        result = ada_bridge.install()
        check("install after failed swap succeeds", result["ok"] is True
              and result["version"] == "9.9.12-test", str(result))
        check("recovered to the new release",
              read(os.path.join(bundle_dest, "marker.txt")) == "bundle-v3")

        # 8. Crash-state matrix (Codex round 2): the exact on-disk states a
        # kill leaves after EACH swap rename. An install attempt with the
        # CDN unreachable must FIRST restore the parked components — before
        # any network access — and leave a working old installation even
        # though the install itself fails.
        old_binary = read(ada_bridge.ADA)  # the 9.9.12-test script
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

        journal_path = os.path.join(ada_bridge.INSTALL_DIR, ".ada-install-journal.json")

        def write_journal(had_binary, had_bundle):
            with open(journal_path, "w") as f:
                json.dump({"had_binary": had_binary, "had_bundle": had_bundle,
                           "version": "crash-test"}, f)

        def reset_live():
            write_file(ada_bridge.ADA, old_binary)
            shutil.rmtree(bundle_dest, ignore_errors=True)
            make_bundle(bundle_dest, old_marker)
            for p in (ada_bridge.ADA + ".new", ada_bridge.ADA + ".old", journal_path):
                if os.path.exists(p):
                    os.unlink(p)
            shutil.rmtree(bundle_dest + ".new", ignore_errors=True)
            shutil.rmtree(bundle_dest + ".old", ignore_errors=True)

        # The real flow writes the journal BEFORE the first rename, so
        # every reproducible crash state includes it (had_binary/had_bundle
        # true for these upgrade states).
        def s1():  # killed after: rename(ADA -> ADA.old)
            write_journal(True, True)
            os.rename(ada_bridge.ADA, ada_bridge.ADA + ".old")
            write_file(ada_bridge.ADA + ".new", newbin)
            make_bundle(bundle_dest + ".new", "half")

        def s2():  # killed after: rename(ADA.new -> ADA)
            write_journal(True, True)
            os.rename(ada_bridge.ADA, ada_bridge.ADA + ".old")
            write_file(ada_bridge.ADA, newbin)
            make_bundle(bundle_dest + ".new", "half")

        def s3():  # killed after: rename(bundle -> bundle.old)
            write_journal(True, True)
            os.rename(ada_bridge.ADA, ada_bridge.ADA + ".old")
            write_file(ada_bridge.ADA, newbin)
            os.rename(bundle_dest, bundle_dest + ".old")
            make_bundle(bundle_dest + ".new", "half")

        def s4():  # killed after: rename(bundle.new -> bundle), before cleanup
            write_journal(True, True)
            os.rename(ada_bridge.ADA, ada_bridge.ADA + ".old")
            write_file(ada_bridge.ADA, newbin)
            os.rename(bundle_dest, bundle_dest + ".old")
            make_bundle(bundle_dest, "half")

        good_base_url = ada_bridge.BASE_URL
        for label, build_state in (("after binary parked", s1),
                                   ("after new binary live", s2),
                                   ("after bundle parked", s3),
                                   ("after full swap, backups parked", s4)):
            reset_live()
            build_state()
            ada_bridge.BASE_URL = "file://" + os.path.join(root, "no-such-cdn")
            result = ada_bridge.install()
            restored = (result["ok"] is False
                        and read(ada_bridge.ADA) == old_binary
                        and read(os.path.join(bundle_dest, "marker.txt")) == old_marker
                        and not os.path.exists(ada_bridge.ADA + ".old")
                        and not os.path.exists(ada_bridge.ADA + ".new")
                        and not os.path.exists(bundle_dest + ".old")
                        and not os.path.exists(bundle_dest + ".new")
                        and not os.path.exists(journal_path))
            check("crash recovery (%s): old install restored before network" % label,
                  restored, str(result))
        ada_bridge.BASE_URL = good_base_url

        # 9. Crash state + reachable CDN: recover, then upgrade normally.
        reset_live()
        s3()
        publish_release(cdn, "9.9.13-test", FAKE_ADA_OK, bundle_marker="bundle-v4")
        result = ada_bridge.install()
        check("crash state + good CDN: recovers then installs",
              result["ok"] is True and result["version"] == "9.9.13-test"
              and read(os.path.join(bundle_dest, "marker.txt")) == "bundle-v4"
              and not os.path.exists(journal_path),
              str(result))

        # 10. FRESH-install crash states (Codex round 3): no .old exists, so
        # recovery must rely on the journal's had_*=false and roll back to
        # "not installed" — never leave a binary without its bundle.
        def wipe_install():
            for p in (ada_bridge.ADA, ada_bridge.ADA + ".new",
                      ada_bridge.ADA + ".old", journal_path):
                if os.path.exists(p):
                    os.unlink(p)
            for d in (bundle_dest, bundle_dest + ".new", bundle_dest + ".old"):
                shutil.rmtree(d, ignore_errors=True)

        def clean_after_fresh_crash():
            return (not os.path.exists(ada_bridge.ADA)
                    and not os.path.isdir(bundle_dest)
                    and not os.path.exists(ada_bridge.ADA + ".new")
                    and not os.path.isdir(bundle_dest + ".new")
                    and not os.path.exists(journal_path))

        # Codex's exact reproduction: killed after the new binary went live,
        # before the bundle did — with the CDN unreachable afterwards.
        wipe_install()
        write_journal(False, False)
        write_file(ada_bridge.ADA, newbin)
        make_bundle(bundle_dest + ".new", "half")
        ada_bridge.BASE_URL = "file://" + os.path.join(root, "no-such-cdn")
        result = ada_bridge.install()
        check("fresh crash (binary live, bundle staged): rolled back to not-installed",
              result["ok"] is False and clean_after_fresh_crash(), str(result))

        # Killed after both went live but before the journal delete.
        wipe_install()
        write_journal(False, False)
        write_file(ada_bridge.ADA, newbin)
        make_bundle(bundle_dest, "half")
        result = ada_bridge.install()
        check("fresh crash (both live, journal not cleared): rolled back to not-installed",
              result["ok"] is False and clean_after_fresh_crash(), str(result))

        # Staging-only crash (journal never written): just staging cleanup.
        wipe_install()
        write_file(ada_bridge.ADA + ".new", newbin)
        make_bundle(bundle_dest + ".new", "half")
        result = ada_bridge.install()
        check("fresh crash (staging only, no journal): cleaned to not-installed",
              result["ok"] is False and clean_after_fresh_crash(), str(result))
        ada_bridge.BASE_URL = good_base_url

        # Fresh crash + reachable CDN: recovery, then a clean first install.
        wipe_install()
        write_journal(False, False)
        write_file(ada_bridge.ADA, newbin)
        make_bundle(bundle_dest + ".new", "half")
        result = ada_bridge.install()
        check("fresh crash + good CDN: recovers then installs",
              result["ok"] is True and result["version"] == "9.9.13-test"
              and read(os.path.join(bundle_dest, "marker.txt")) == "bundle-v4"
              and not os.path.exists(journal_path), str(result))

        # 11. No journal + stray .old = post-success garbage: the live
        # (validated) install stays, the .old is deleted, NOT restored —
        # partial restoration here is what created mixed-version installs.
        live_binary = read(ada_bridge.ADA)
        write_file(ada_bridge.ADA + ".old", "#!/bin/sh\necho stale\n")
        make_bundle(bundle_dest + ".old", "stale")
        publish_release(cdn, "9.9.14-test", FAKE_ADA_OK, bundle_marker="bundle-v5")
        result = ada_bridge.install()
        check("stray .old without journal: dropped as garbage, install proceeds",
              result["ok"] is True and result["version"] == "9.9.14-test"
              and not os.path.exists(ada_bridge.ADA + ".old")
              and not os.path.isdir(bundle_dest + ".old"), str(result))
        del live_binary

        # 12. Interrupting RECOVERY itself (Codex round 5): if the repairs
        # cannot be made durable, the journal must survive so the next run
        # retries — a lost journal over non-durable repairs would make a
        # later boot treat the half-swap as completed.
        real_fsync_dir = ada_bridge._fsync_dir
        wipe_install()
        write_journal(False, False)
        write_file(ada_bridge.ADA, newbin)
        make_bundle(bundle_dest + ".new", "half")
        ada_bridge._fsync_dir = lambda path: "injected: I/O error"
        result = ada_bridge.install()
        ada_bridge._fsync_dir = real_fsync_dir
        check("recovery barrier failure: reported, journal retained for retry",
              result["ok"] is False
              and "durable" in (result["error"] or "")
              and os.path.exists(journal_path), str(result))
        result = ada_bridge.install()
        check("recovery retry after barrier failure: repairs commit, install succeeds",
              result["ok"] is True and result["version"] == "9.9.14-test"
              and not os.path.exists(journal_path), str(result))

        # Idempotency: running recovery twice against the same upgrade
        # crash state must converge to the same clean restored state.
        old_binary2 = read(ada_bridge.ADA)
        reset2_marker = "bundle-v5"
        for attempt in (1, 2):
            if attempt == 1:
                # construct S3-style state against the CURRENT install
                write_journal(True, True)
                os.rename(ada_bridge.ADA, ada_bridge.ADA + ".old")
                write_file(ada_bridge.ADA, newbin)
                os.rename(bundle_dest, bundle_dest + ".old")
                make_bundle(bundle_dest + ".new", "half")
            ada_bridge.BASE_URL = "file://" + os.path.join(root, "no-such-cdn")
            result = ada_bridge.install()
            check("recovery idempotent (run %d): restored state stable" % attempt,
                  result["ok"] is False
                  and read(ada_bridge.ADA) == old_binary2
                  and read(os.path.join(bundle_dest, "marker.txt")) == reset2_marker
                  and not os.path.exists(journal_path)
                  and not os.path.exists(ada_bridge.ADA + ".old")
                  and not os.path.isdir(bundle_dest + ".old"), str(result))
        ada_bridge.BASE_URL = good_base_url

        # 13. Final commit sync failure (Codex round 6): if the post-unlink
        # directory sync fails, BOTH .old backups must survive and the
        # result must be an honest failure — deleting backups while the
        # journal's fate is uncertain opens a mixed-version reboot. The
        # injected wrapper fails only once the journal is gone, so the
        # earlier barriers run for real.
        publish_release(cdn, "9.9.15-test", FAKE_ADA_OK, bundle_marker="bundle-v6")
        pre_upgrade_binary = read(ada_bridge.ADA)

        def fail_after_unlink(path):
            if not os.path.exists(journal_path):
                return "injected: commit sync I/O error"
            return real_fsync_dir(path)

        ada_bridge._fsync_dir = fail_after_unlink
        result = ada_bridge.install()
        ada_bridge._fsync_dir = real_fsync_dir
        check("commit sync failure: honest error, BOTH backups retained, new install live",
              result["ok"] is False and "durable" in (result["error"] or "")
              and os.path.isfile(ada_bridge.ADA + ".old")
              and os.path.isdir(bundle_dest + ".old")
              and read(ada_bridge.ADA + ".old") == pre_upgrade_binary
              and "9.9.15-test" in read(ada_bridge.ADA)
              and not os.path.exists(journal_path), str(result))
        # Reboot outcome B (journal absent): both backups discarded as
        # garbage, the validated new install stays. (Outcome A — journal
        # present restores BOTH old components — is section 8's s4 case.)
        result = ada_bridge.install()
        check("retry after commit-sync failure: backups dropped, install settles",
              result["ok"] is True and result["version"] == "9.9.15-test"
              and not os.path.exists(ada_bridge.ADA + ".old")
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
            result = ada_bridge.run_sudo_command(
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

            result = ada_bridge.run_sudo_command("", "1234")
            check("run_sudo_command: empty command refused without spawning",
                  result["ok"] is False and "empty" in result["error"], str(result))

            result = ada_bridge.run_sudo_command("sudo true", "wrong")
            check("run_sudo_command: sudo exit 1 reported as passcode/refusal",
                  result["ok"] is False and "passcode" in result["error"], str(result))

            # run_privileged_script: 0600 temp file, deleted afterwards.
            for stale in ("sudo_script_copy", "sudo_script_mode", "sudo_script_path"):
                p = os.path.join(fakedir, stale)
                if os.path.exists(p):
                    os.unlink(p)
            script_text = "#!/bin/sh\necho wakelock-install\n"
            result = ada_bridge.run_privileged_script(script_text, "1234")
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

            result = ada_bridge.run_privileged_script("   ", "1234")
            check("run_privileged_script: empty script refused",
                  result["ok"] is False and "empty" in result["error"], str(result))

            # default_linger_command: local fallback for CLI releases that
            # don't serve service.linger_command in status (v0.1.43).
            linger = ada_bridge.default_linger_command()
            import getpass as _getpass
            check("default_linger_command: sudo loginctl for the current user",
                  linger == "sudo loginctl enable-linger " + _getpass.getuser(),
                  linger)

            # systemctl_user: whitelist + argv shape.
            result = ada_bridge.systemctl_user("start")
            check("systemctl_user start: ok + exact argv",
                  result["ok"] is True
                  and read(os.path.join(fakedir, "systemctl_args")).splitlines()
                      == ["--user", "start", "ada.service"], str(result))
            os.unlink(os.path.join(fakedir, "systemctl_args"))
            result = ada_bridge.systemctl_user("restart")
            check("systemctl_user: non-whitelisted action refused without spawning",
                  result["ok"] is False
                  and not os.path.exists(os.path.join(fakedir, "systemctl_args")),
                  str(result))

            # tail_journal: bounded count, unit-scoped argv.
            result = ada_bridge.tail_journal(10)
            journal_args = read(os.path.join(fakedir, "journalctl_args")).splitlines()
            check("tail_journal: ok + lines",
                  result["ok"] is True and "log line two" in result["text"], str(result))
            check("tail_journal: --user, unit, -n argv",
                  journal_args[:5] == ["--user", "-u", "ada.service", "-n", "10"],
                  str(journal_args))
            result = ada_bridge.tail_journal("bogus")
            check("tail_journal: non-numeric count falls back to default",
                  result["ok"] is True and
                  read(os.path.join(fakedir, "journalctl_args")).splitlines()[4] == "40",
                  str(result))

            # Missing binaries (fakebin off PATH): honest errors, no crash.
            os.environ["PATH"] = os.path.join(root, "empty-path")
            result = ada_bridge.tail_journal(5)
            check("tail_journal: journalctl missing → honest error",
                  result["ok"] is False and "not found" in result["error"], str(result))
            result = ada_bridge.run_sudo_command("sudo true", "x")
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
        os.environ["ADA_UT_APP_SETTINGS_PATH"] = settings_path
        os.environ["ADA_UT_APP_MANIFEST"] = own_manifest
        old_app_base = ada_bridge.APP_BASE_URL
        ada_bridge.APP_BASE_URL = "file://" + app_cdn

        def publish_app(version, data=b"click-bytes", **overrides):
            click_name = "ada.permaevidence_%s_all.click" % version
            with open(os.path.join(app_cdn, click_name), "wb") as f:
                f.write(data)
            manifest = {"version": version, "filename": click_name,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "size": len(data)}
            manifest.update(overrides)
            with open(os.path.join(app_cdn, "manifest.json"), "w") as f:
                json.dump(manifest, f)

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
        os.environ["ADA_UT_CLICK_DBUS_TOOL"] = "none"
        try:
            # Settings: defaults, persistence, unknown-key refusal.
            check("app_settings: default auto_update off",
                  ada_bridge.app_settings() == {"auto_update": False}, "")
            result = ada_bridge.set_app_setting("auto_update", True)
            check("set_app_setting: persists and echoes",
                  result["ok"] is True
                  and result["settings"]["auto_update"] is True
                  and ada_bridge.app_settings()["auto_update"] is True,
                  str(result))
            result = ada_bridge.set_app_setting("bogus", True)
            check("set_app_setting: unknown key refused",
                  result["ok"] is False, str(result))
            with open(settings_path, "w") as f:
                f.write("{corrupt")
            check("app_settings: corrupt file → defaults",
                  ada_bridge.app_settings() == {"auto_update": False}, "")
            ada_bridge.set_app_setting("auto_update", False)

            # Version comparison: strictly-newer only, unparseable never.
            check("_version_newer: newer/equal/older/unparseable",
                  ada_bridge._version_newer("0.5.1", "0.5.0") is True
                  and ada_bridge._version_newer("0.5.0", "0.5.0") is False
                  and ada_bridge._version_newer("0.4.9", "0.5.0") is False
                  and ada_bridge._version_newer("v0.10.0", "0.9.9") is True
                  and ada_bridge._version_newer("beta", "0.5.0") is False
                  and ada_bridge._version_newer("0.6.0", "junk") is False, "")

            # Check: current, newer, malformed, suspicious filename.
            publish_app("0.5.0")
            result = ada_bridge.app_update_check()
            check("app_update_check: same version → no update",
                  result["ok"] is True and result["update_available"] is False
                  and result["installed"] == "0.5.0", str(result))
            publish_app("0.6.0")
            result = ada_bridge.app_update_check()
            check("app_update_check: newer version detected",
                  result["ok"] is True and result["update_available"] is True
                  and result["available"] == "0.6.0", str(result))
            publish_app("0.6.0", size="huge")
            result = ada_bridge.app_update_check()
            check("app_update_check: malformed manifest refused",
                  result["ok"] is False and "malformed" in result["error"],
                  str(result))
            publish_app("0.6.0", filename="../evil.click")
            result = ada_bridge.app_update_check()
            check("app_update_check: traversal filename refused",
                  result["ok"] is False and "filename" in result["error"],
                  str(result))

            # Install: happy path — pkcon called correctly, staging cleaned.
            publish_app("0.6.0")
            result = ada_bridge.app_update_install()
            pkcon_args = read(pkcon_out).splitlines()
            staged = os.path.join(os.path.dirname(settings_path),
                                  "ada.permaevidence_0.6.0_all.click")
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
            result = ada_bridge.app_update_install()
            check("app_update_install: checksum mismatch refused, no pkcon",
                  result["ok"] is False and "checksum" in result["error"]
                  and not os.path.exists(pkcon_out), str(result))

            # Install: size mismatch → refused, no pkcon.
            publish_app("0.6.0", size=1)
            result = ada_bridge.app_update_install()
            check("app_update_install: size mismatch refused, no pkcon",
                  result["ok"] is False and "size mismatch" in result["error"]
                  and not os.path.exists(pkcon_out), str(result))

            # Install: pkcon failure surfaces honestly, staging still cleaned.
            publish_app("0.6.0")
            os.environ["FAKE_PKCON_EXIT"] = "5"
            result = ada_bridge.app_update_install()
            check("app_update_install: pkcon failure → honest error + cleanup",
                  result["ok"] is False
                  and "pkcon failed (exit 5)" in result["error"]
                  and not os.path.exists(staged), str(result))
            os.environ.pop("FAKE_PKCON_EXIT", None)

            # pkcon resolution: Lomiri apps get a slimmer PATH than the
            # Terminal (field bug: bare "pkcon" → 127), so a pkcon that is
            # NOT in PATH must still be found via absolute candidates.
            fake_pkcon = os.path.join(pkcon_bin, "pkcon")
            old_candidates = ada_bridge.PKCON_CANDIDATES
            os.environ["PATH"] = "/nonexistent-path-entry"
            try:
                ada_bridge.PKCON_CANDIDATES = (fake_pkcon,)
                publish_app("0.6.0")
                result = ada_bridge.app_update_install()
                check("app_update_install: pkcon found via absolute "
                      "candidate when PATH lacks it",
                      result["ok"] is True and result["updated"] is True,
                      str(result))
                # Genuinely absent: 127 with a diagnostic (PATH + fallback
                # command) and the staged click still cleaned up.
                ada_bridge.PKCON_CANDIDATES = (
                    os.path.join(app_root, "no-such-pkcon"),)
                publish_app("0.6.0")
                result = ada_bridge.app_update_install()
                check("app_update_install: pkcon truly missing → "
                      "Morph/OpenStore advice + cleanup",
                      result["ok"] is False
                      and "pkcon is not installed" in result["error"]
                      and "Morph" in result["error"]
                      and "install-local" not in result["error"]
                      and not os.path.exists(staged), str(result))
            finally:
                ada_bridge.PKCON_CANDIDATES = old_candidates
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
            os.environ["ADA_UT_CLICK_DBUS_TOOL"] = fake_busctl
            os.environ["PATH"] = "/nonexistent-path-entry"
            old_cand2 = ada_bridge.PKCON_CANDIDATES
            ada_bridge.PKCON_CANDIDATES = (
                os.path.join(app_root, "no-such-pkcon"),)
            try:
                publish_app("0.6.0")
                result = ada_bridge.app_update_install()
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
                result = ada_bridge.app_update_install()
                check("app_update_install: D-Bus fail + no pkcon → both "
                      "attempts listed + Morph advice + cleanup",
                      result["ok"] is False
                      and "com.lomiri.click" in result["error"]
                      and "Morph" in result["error"]
                      and not os.path.exists(staged), str(result))

                # D-Bus fail on an image that still HAS pkcon → fallback.
                ada_bridge.PKCON_CANDIDATES = (
                    os.path.join(pkcon_bin, "pkcon"),)
                publish_app("0.6.0")
                result = ada_bridge.app_update_install()
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
                os.environ["ADA_UT_CLICK_REGISTRY"] = registry
                os.environ["ADA_UT_CLICK_REGISTRY_WAIT"] = "0"
                publish_app("0.6.0")
                result = ada_bridge.app_update_install()
                check("app_update_install: stale registry after 'success' "
                      "→ honest error",
                      result["ok"] is False
                      and "registry" in result["error"], str(result))
                with open(os.path.join(registry, "current",
                                       "manifest.json"), "w") as f:
                    json.dump({"version": "0.6.0"}, f)
                publish_app("0.6.0")
                result = ada_bridge.app_update_install()
                check("app_update_install: registry reaches target → "
                      "verified ok",
                      result["ok"] is True and result["updated"] is True,
                      str(result))
            finally:
                ada_bridge.PKCON_CANDIDATES = old_cand2
                os.environ["PATH"] = pkcon_bin + os.pathsep + old_path
                os.environ["ADA_UT_CLICK_DBUS_TOOL"] = "none"
                os.environ.pop("FAKE_BUSCTL_EXIT", None)
                os.environ.pop("ADA_UT_CLICK_REGISTRY", None)
                os.environ.pop("ADA_UT_CLICK_REGISTRY_WAIT", None)

            # Install: already current → updated False, no pkcon call.
            os.unlink(pkcon_out) if os.path.exists(pkcon_out) else None
            publish_app("0.5.0")
            result = ada_bridge.app_update_install()
            check("app_update_install: already current → no-op",
                  result["ok"] is True and result["updated"] is False
                  and not os.path.exists(pkcon_out), str(result))

            # Auto-update: off → no run AND no network (dead CDN proves it).
            ada_bridge.APP_BASE_URL = "file://" + os.path.join(
                app_root, "no-such-cdn")
            result = ada_bridge.app_auto_update()
            check("app_auto_update: off → ran False, no fetch",
                  result == {"ran": False}, str(result))
            # Auto-update: on → runs the chain (dead CDN → honest error).
            ada_bridge.set_app_setting("auto_update", True)
            result = ada_bridge.app_auto_update()
            check("app_auto_update: on → ran True, surfaces fetch error",
                  result["ran"] is True and result["ok"] is False
                  and "update server" in result["error"], str(result))
            ada_bridge.APP_BASE_URL = "file://" + app_cdn
            publish_app("0.6.0")
            result = ada_bridge.app_auto_update()
            check("app_auto_update: on → installs newer version",
                  result["ran"] is True and result["ok"] is True
                  and result["updated"] is True, str(result))
        finally:
            os.environ["PATH"] = old_path
            os.environ.pop("FAKE_PKCON_EXIT", None)
            os.environ.pop("ADA_UT_CLICK_DBUS_TOOL", None)
            os.environ.pop("ADA_UT_APP_SETTINGS_PATH", None)
            os.environ.pop("ADA_UT_APP_MANIFEST", None)
            ada_bridge.APP_BASE_URL = old_app_base
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\nbridge selftest: %d passed, %d failed" % (PASSED, FAILED))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
