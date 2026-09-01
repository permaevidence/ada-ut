#!/usr/bin/env python3
"""Repository invariants for the product identity (rename plan §5).

Static, offline, seconds. Pins the things a rename can silently get wrong:

  1. The click identity is ONE thing in ONE place: manifest.json's package
     name is what Main.qml's applicationName, the hook filenames and the
     publisher's derived click filename all follow.
  2. The previous identity ("ada") survives ONLY where it is meant to:
     the bridge's LEGACY_* detection block, the legacy keep-awake removal
     script, and the migration copy the user reads (MigratePage + the two
     entry points that lead there). Nowhere else in shipped files — no
     old env seam, socket path, unit name, bundle name, package id or
     website host.
  3. What must NOT change with the rename did not: the signed-envelope
     format name, the QR bundle prefix, the pinned key hexes.
  4. The migration UX is wired: routing and the post-install path consult
     the CLI's migration gate before the wizard; MigratePage reaches the
     bridge's `migrate` and `swap_legacy_wakelock`; the Dashboard offers
     the pending keep-awake swap.

Usage: python3 scripts/identity_selftest.py
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "py"))
import briglia_bridge  # noqa: E402
import release_verify  # noqa: E402

PASSED = FAILED = 0


def check(label, ok, detail=""):
    global PASSED, FAILED
    print("%s %s%s" % ("PASS" if ok else "FAIL", label,
                       "" if ok or not detail else " — " + str(detail)[:400]))
    if ok:
        PASSED += 1
    else:
        FAILED += 1


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def shipped_files():
    """Everything build_click.py packages, plus the manifest."""
    out = ["manifest.json"]
    for top in ("click", "qml", "py"):
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, top)):
            for name in files:
                if name.endswith((".pyc",)) or "__pycache__" in dirpath:
                    continue
                out.append(os.path.relpath(os.path.join(dirpath, name), ROOT))
    return sorted(out)


def main():
    manifest = json.loads(read("manifest.json"))
    pkg = manifest["name"]
    hook = list(manifest["hooks"].keys())

    # ---- 1. one identity
    check("manifest: package name is briglia.permaevidence", pkg == "briglia.permaevidence", pkg)
    check("manifest: title is Briglia", manifest["title"] == "Briglia", manifest["title"])
    check("manifest: exactly one hook, named after the product",
          hook == ["briglia"], hook)
    for kind in ("apparmor", "desktop"):
        rel = manifest["hooks"][hook[0]][kind]
        check("manifest: %s hook file exists (%s)" % (kind, rel), os.path.isfile(os.path.join(ROOT, rel)), rel)
    main_qml = read("qml/Main.qml")
    m = re.search(r'applicationName:\s*"([^"]+)"', main_qml)
    check("Main.qml applicationName == manifest name", m and m.group(1) == pkg, m and m.group(1))
    desktop = read(manifest["hooks"][hook[0]]["desktop"])
    check("desktop entry names Briglia", "Name=Briglia" in desktop and "Ada" not in desktop, desktop)
    publisher = read("scripts/publish_click.sh")
    check("publisher derives the click filename from manifest.json (no hardcoded package id)",
          "PKG_NAME=" in publisher and 'CLICK="build/${PKG_NAME}_${VERSION}_all.click"' in publisher
          and "permaevidence_${VERSION}" not in publisher)
    check("publisher title/channel are Briglia's",
          'CHANNEL="briglia-ut"' in publisher and 'TITLE="Briglia for Ubuntu Touch"' in publisher)
    check("app cache/state paths follow the package id",
          "~/.cache/briglia.permaevidence" in read("py/chat_client.py")
          and "~/.cache/briglia.permaevidence" in read("py/voice_record.py")
          and "~/.cache/briglia.permaevidence" in read("py/briglia_bridge.py")
          and "/opt/click.ubuntu.com/briglia.permaevidence" in read("py/briglia_bridge.py"))
    check("website base: QML constant == bridge constant",
          ('websiteBase: "%s"' % briglia_bridge.WEBSITE_BASE) in main_qml, briglia_bridge.WEBSITE_BASE)

    # ---- 2. the previous identity, confined
    forbidden = [
        "ada.permaevidence", "ada_bridge", "ADA_UT_", "ADA_CHAT_SOCKET", "ADA_QR_",
        "ada-app-psi", ".local/bin/ada\"", ".config/ada\"", ".local/share/ada",
        "ada.service", "ada-keepawake", "ada-cli_ada", "\"ada-cli\"", "\"ada-ut\"",
        "permaevidence/ada-cli", "permaevidence/ada-ut", ".config/ada-ut", "get-ada.sh",
    ]
    offenders = []
    for rel in shipped_files():
        text = read(rel) if not rel.endswith(".png") else ""
        for ln, line in enumerate(text.splitlines(), 1):
            for token in forbidden:
                if token in line:
                    if rel == "py/briglia_bridge.py" and "LEGACY_" in line:
                        continue  # the detection block / removal script
                    offenders.append("%s:%d %s" % (rel, ln, token))
    check("no retired identifier outside the bridge's LEGACY_ block", not offenders, offenders)

    word_ada = re.compile(r"\bAda\b")
    allowed_prose = {"qml/pages/MigratePage.qml", "qml/Main.qml", "qml/pages/DashboardPage.qml",
                     "py/briglia_bridge.py"}
    prose_offenders = []
    for rel in shipped_files():
        if rel in allowed_prose or rel.endswith(".png"):
            continue
        for ln, line in enumerate(read(rel).splitlines(), 1):
            if word_ada.search(line):
                prose_offenders.append("%s:%d" % (rel, ln))
    check("the word 'Ada' appears only on the migration surfaces", not prose_offenders, prose_offenders)
    # …and on those surfaces only in migration context, never as the product.
    for rel in ("qml/Main.qml", "qml/pages/DashboardPage.qml"):
        bad = [ln for ln, line in enumerate(read(rel).splitlines(), 1)
               if word_ada.search(line) and not re.search(r"Ada (CLI installation|data|configuration)", line)]
        check("%s: 'Ada' only in migration copy" % rel, not bad, bad)
    bridge = read("py/briglia_bridge.py")
    check("bridge: every legacy path literal sits on a LEGACY_ line",
          all("LEGACY_" in line for line in bridge.splitlines()
              if re.search(r'"[^"]*(\.config/ada|share/ada|bin/ada|ada-keepawake|systemd/user/ada)', line)))
    check("bridge: setup-api schema is exactly 2", briglia_bridge.SETUP_API_SCHEMA == 2)
    check("bridge: binary and bundle names", briglia_bridge.BRIGLIA.endswith("/.local/bin/briglia")
          and briglia_bridge.BUNDLE_NAME == "briglia-cli_briglia.resources")
    check("bridge: install journal name follows the product",
          briglia_bridge._journal_path().endswith("/.briglia-install-journal.json"))
    check("bridge: legacy removal script targets the legacy unit only",
          briglia_bridge.LEGACY_WAKELOCK_UNIT_NAME in briglia_bridge.legacy_wakelock_uninstall_script()
          and "briglia-keepawake" not in briglia_bridge.legacy_wakelock_uninstall_script())

    # ---- 3. what must not change
    check("signed-envelope format name is unchanged (historical, plan §3)",
          release_verify.FORMAT == "ada-release-envelope-v1")
    for rel in ("scripts/release/sign-envelope.sh", "scripts/release/verify-envelope.sh"):
        check("%s keeps the historical format name" % rel, "ada-release-envelope-v1" in read(rel))
        check("%s names no retired channel" % rel,
              "ada-cli" not in read(rel).replace("ada-release-envelope-v1", "")
              and "ada-ut" not in read(rel))
    for rel in ("scripts/release/sign-envelope.sh", "scripts/release/release-keygen.sh"):
        check("%s allows only the Briglia channels" % rel,
              "briglia-cli|briglia-ut" in read(rel) or "briglia-ut|briglia-cli" in read(rel))
    import qr_scan  # noqa: E402
    check("QR bundle prefix unchanged", qr_scan.BUNDLE_PREFIX == "ADAK1:")
    check("CLI key hex unchanged, keyId re-derived under briglia-cli",
          release_verify.CLI_KEYS == {"briglia-cli-release-v1-94d967bae0867c2e":
                                      "621031636aa2bb2edb64a58f2f72de7bc3559b08d717c79b4251f8b1e35b8a95"})
    check("app key hex unchanged, keyId re-derived under briglia-ut",
          release_verify.APP_KEYS == {"briglia-ut-release-v1-7bb0163ac16c5cb3":
                                      "cdfa5dba857ad9276f2630c0c7028b53ea9933cc969e69f0a1cff4727ff0b7dc"})
    check("channels/repos/trust file are Briglia's",
          release_verify.CLI_POLICY.channel == "briglia-cli"
          and release_verify.APP_POLICY.channel == "briglia-ut"
          and "permaevidence/briglia-cli/releases" in release_verify.CLI_POLICY.envelope_url
          and "permaevidence/briglia-ut/releases" in release_verify.APP_POLICY.artifact_url_prefix
          and release_verify.TRUST_FILE.endswith("/.config/briglia-ut/release_trust.json"))
    check("sequences continue across the rename (CLI ≥ 60, app ≥ 2, this build = 2)",
          release_verify.MIN_CLI_SEQUENCE >= 60 and release_verify.MIN_APP_SEQUENCE >= 2
          and release_verify.APP_RELEASE_SEQUENCE >= release_verify.MIN_APP_SEQUENCE)
    check("User-Agent follows the product", release_verify.USER_AGENT == "briglia-ut-app")
    check("committed public key file follows the product",
          os.path.isfile(os.path.join(ROOT, ".release-keys", "briglia-ut-release.pub.pem"))
          and not os.path.exists(os.path.join(ROOT, ".release-keys", "ada-ut-release.pub.pem")))
    check("manifest version is the first Briglia app release line (0.8.x)",
          manifest["version"].startswith("0.8."), manifest["version"])

    # ---- 4. migration UX wiring
    check("Main.qml: migrationNeeded derives from the CLI's status block, never from paths",
          "api.migration.needed === true" in main_qml and "readonly property bool migrationNeeded" in main_qml)
    route = main_qml.split("function routeInitial()")[1].split("function openMigrate")[0]
    check("Main.qml: routeInitial consults the migration gate BEFORE setup/wizard routing",
          "if (migrationNeeded) { openMigrate(); return; }" in route
          and route.index("migrationNeeded") < route.index("startWizard()"))
    finish = main_qml.split("function finishAfterInstall()")[1].split("}", 1)[0] + main_qml.split("function finishAfterInstall()")[1][:400]
    check("Main.qml: post-install path consults the migration gate before the wizard",
          "if (root.migrationNeeded) { root.openMigrate(); return; }" in finish)
    check("Main.qml: post-install restart is skipped while a migration is pending",
          "if (root.migrationNeeded) {" in main_qml.split("py.call(\"briglia_bridge.install\"")[1][:900])
    check("Main.qml: boot page offers the migrating install and the explicit migrate entry",
          "Install Briglia CLI (migrates Ada data)" in main_qml and "Migrate Ada data to Briglia" in main_qml)
    migrate_qml = read("qml/pages/MigratePage.qml")
    check("MigratePage: consent button reaches the bridge's migrate (forward) and rollback",
          'pyCall("migrate", [rollback === true]' in migrate_qml
          and 'text: i18n.tr("Migrate now")' in migrate_qml and 'text: i18n.tr("Roll back to Ada")' in migrate_qml)
    check("MigratePage: conflict is explained and never auto-resolved",
          "migration.conflict === true" in migrate_qml and "Check again" in migrate_qml
          and "rmtree" not in migrate_qml and "unlink" not in migrate_qml)
    check("MigratePage: keep-awake swap runs through swap_legacy_wakelock with the passcode dialog",
          'pyCall("swap_legacy_wakelock", [passcode]' in migrate_qml
          and "echoMode: TextInput.Password" in migrate_qml)
    check("MigratePage: rollback offered only before the commit point",
          '["prepared", "moved", "fixups"].indexOf(journalState) !== -1' in migrate_qml)
    dash = read("qml/pages/DashboardPage.qml")
    check("Dashboard: offers the pending migration and the pending keep-awake swap",
          'onClicked: page.app.openMigrate()' in dash and 'openMigrate("wakelock")' in dash
          and "legacyWakelockPresent" in dash)
    check("Dashboard: wizard entry hidden while a migration is pending",
          "!page.app.migrationNeeded\n                         && !(page.api.setup" in dash)
    identity = read("qml/pages/IdentityPage.qml")
    check("IdentityPage: persona sentence names Bree as the customizable default",
          'stays \\"Bree\\" (you can change it)' in identity)
    check("chat gate: Briglia CLI 0.2.0 baseline",
          'versionAtLeast(detectInfo.version, "0.2.0")' in main_qml
          and "Briglia CLI 0.2.0 or newer" in dash)
    readme = read("README.md")
    check("README: requires Briglia CLI ≥ v0.2.0 and documents old-app coexistence",
          "≥ v0.2.0" in readme and "ada.permaevidence" in readme and "cannot self-update" in readme)

    print("\nidentity selftest: %d passed, %d failed" % (PASSED, FAILED))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
