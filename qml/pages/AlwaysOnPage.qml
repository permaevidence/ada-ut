import QtQuick 2.12
import QtQuick.Layouts 1.12
import Lomiri.Components 1.3
import Lomiri.Components.Popups 1.3

/*
 * Screen 7 — Always-on (UT_APP_PLAN.md §2.4 #7 + the service slice of
 * Settings). Enables the systemd user service (`ada daemon`, Restart=always,
 * starts at boot via linger) and, on Ubuntu Touch, the kernel wakelock unit
 * that keeps Ada alive with the screen off. Root steps run the scripts
 * ada-cli itself serves (setup-api keepawake_script / linger_command) under
 * `sudo -S`; the passcode is collected in a dialog, travels only on sudo's
 * stdin, and is never stored (§2.5).
 */
Page {
    id: page
    property var app
    property bool wizardMode: false

    header: PageHeader {
        title: page.wizardMode ? i18n.tr("Step 5 · Always on") : i18n.tr("Background service")
    }

    readonly property var service: app.api && app.api.service ? app.api.service : null
    readonly property bool telegramReady: app.api && app.api.telegram
                                          && app.api.telegram.configured === true
    readonly property bool isUT: app.api && app.api.is_ubuntu_touch === true
    readonly property bool serviceSupported: service !== null && service.supported === true

    property bool working: false
    property string resultText: ""
    property bool resultIsError: false

    // "Enable start at boot" must be resumable at ANY time (a closed app,
    // a cancelled dialog, or a failed sudo must not strand it): the button
    // derives from the status block, not from install-response memory.
    // Command preference: status block (CLI ≥ 0.1.44) → last install
    // response → local fallback with the same text.
    property string installLingerCommand: ""   // from the install response
    property string fallbackLingerCommand: ""
    readonly property bool lingerMissing: serviceSupported && service
                                          && service.unit_installed === true
                                          && service.linger === false
    readonly property string lingerCommand: {
        if (service && service.linger_command) return service.linger_command;
        if (installLingerCommand !== "") return installLingerCommand;
        return fallbackLingerCommand;
    }

    Component.onCompleted: {
        app.pyCall("default_linger_command", [], function(result) {
            if (result) page.fallbackLingerCommand = result;
        });
    }

    function fail(text) { working = false; resultIsError = true; resultText = text; }
    function succeed(text) {
        working = false; resultIsError = false; resultText = text;
        app.refresh();
    }

    // ------------------------------------------------------- passcode dialog
    property var pendingPrivileged: null   // function(passcode)
    property string pendingLabel: ""

    Component {
        id: passcodeDialog
        Dialog {
            id: dialog
            title: i18n.tr("Device passcode needed")
            text: page.pendingLabel
            TextField {
                id: passField
                echoMode: TextInput.Password
                inputMethodHints: Qt.ImhSensitiveData | Qt.ImhNoPredictiveText
                placeholderText: i18n.tr("Passcode")
            }
            Button {
                color: theme.palette.normal.positive
                text: i18n.tr("Run")
                onClicked: {
                    var passcode = passField.text;
                    passField.text = "";
                    PopupUtils.close(dialog);
                    var run = page.pendingPrivileged;
                    page.pendingPrivileged = null;
                    if (run) run(passcode);
                }
            }
            Button {
                text: i18n.tr("Cancel")
                onClicked: {
                    passField.text = "";
                    page.pendingPrivileged = null;
                    page.working = false;
                    PopupUtils.close(dialog);
                }
            }
        }
    }

    function askPasscode(label, run) {
        pendingLabel = label;
        pendingPrivileged = run;
        PopupUtils.open(passcodeDialog, page);
    }

    // ------------------------------------------------------- service actions
    function enableService() {
        working = true; resultIsError = false;
        resultText = i18n.tr("Installing the background service…");
        app.apiService({action: "install"}, function(result) {
            if (!result || result.ok !== true) {
                page.fail(page.app.describeError(result));
                return;
            }
            if (result.linger_command) {
                page.installLingerCommand = result.linger_command;
                page.succeed(i18n.tr("Service installed. One more step below: allow Ada to start at boot."));
            } else {
                page.succeed(i18n.tr("Service installed — Ada now runs in the background and starts at boot."));
            }
        });
    }

    function grantLinger() {
        working = true; resultIsError = false;
        askPasscode(i18n.tr("Allows Ada to start at boot without you logging in. Runs: %1").arg(lingerCommand),
                    function(passcode) {
            page.resultText = i18n.tr("Enabling start-at-boot…");
            page.app.pyCall("run_sudo_command", [page.lingerCommand, passcode], function(result) {
                if (!result || result.ok !== true) {
                    page.fail(result && result.error ? result.error : i18n.tr("sudo failed"));
                    return;
                }
                page.installLingerCommand = "";
                page.succeed(i18n.tr("Done — Ada starts at boot."));
            });
        });
    }

    function disableService() {
        working = true; resultIsError = false;
        resultText = i18n.tr("Removing the background service…");
        app.apiService({action: "uninstall"}, function(result) {
            if (!result || result.ok !== true) {
                page.fail(page.app.describeError(result));
                return;
            }
            page.succeed(i18n.tr("Service removed. Ada no longer runs in the background."));
        });
    }

    function restartService() {
        working = true; resultIsError = false;
        resultText = i18n.tr("Restarting Ada…");
        app.apiService({action: "restart"}, function(result) {
            if (!result || result.ok !== true) {
                page.fail(page.app.describeError(result));
                return;
            }
            page.succeed(i18n.tr("Ada restarted."));
        });
    }

    // ------------------------------------------------------- wakelock
    function setWakelock(enable) {
        working = true; resultIsError = false;
        resultText = i18n.tr("Preparing…");
        app.apiService({keepawake_script: true}, function(result) {
            if (!result || result.ok !== true) {
                page.fail(page.app.describeError(result));
                return;
            }
            var script = enable ? result.wakelock_install_script
                                : result.wakelock_uninstall_script;
            if (!script) {
                page.fail(i18n.tr("The CLI did not return the keep-awake script — update Ada CLI."));
                return;
            }
            // Safety gate: refuse a served script that lacks the read-only
            // restore trap (CLI ≤ 0.1.43 — a failing middle step there
            // exits with / left writable). Content check, not a version
            // compare: it verifies the property that actually matters and
            // needs no parsing.
            if (script.indexOf("trap 'mount -o remount,ro /") === -1) {
                page.fail(i18n.tr("This Ada CLI release serves a keep-awake script that could leave the system partition writable if a step fails. Update Ada CLI (button on the Dashboard) and retry."));
                return;
            }
            page.askPasscode(enable
                ? i18n.tr("Installs the keep-awake unit so the phone doesn't suspend Ada when the screen is off. Briefly remounts the system partition read-write.")
                : i18n.tr("Removes the keep-awake unit. Briefly remounts the system partition read-write."),
                function(passcode) {
                    page.resultText = enable ? i18n.tr("Installing keep-awake…")
                                             : i18n.tr("Removing keep-awake…");
                    page.app.pyCall("run_privileged_script", [script, passcode], function(res) {
                        if (!res || res.ok !== true) {
                            page.fail(res && res.error ? res.error : i18n.tr("sudo failed"));
                            return;
                        }
                        page.succeed(enable
                            ? i18n.tr("Keep-awake active — Ada stays reachable with the screen off.")
                            : i18n.tr("Keep-awake removed."));
                    });
                });
        });
    }

    Flickable {
        anchors { top: page.header.bottom; left: parent.left; right: parent.right; bottom: parent.bottom }
        contentHeight: column.height + units.gu(6)
        clip: true

        ColumnLayout {
            id: column
            anchors { top: parent.top; topMargin: units.gu(2); horizontalCenter: parent.horizontalCenter }
            width: Math.min(parent.width - units.gu(4), units.gu(50))
            spacing: units.gu(1.5)

            Label {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                color: theme.palette.normal.backgroundSecondaryText
                text: i18n.tr("Run Ada as an always-on background service: it starts at boot, restarts if it crashes, and answers on Telegram around the clock.")
            }

            Label {
                Layout.fillWidth: true
                visible: !page.serviceSupported
                wrapMode: Text.WordWrap
                color: theme.palette.normal.negative
                text: i18n.tr("Background service management is available on Linux/Ubuntu Touch only.")
            }

            Label {
                Layout.fillWidth: true
                visible: page.serviceSupported && !page.telegramReady
                wrapMode: Text.WordWrap
                color: theme.palette.normal.backgroundSecondaryText
                text: i18n.tr("Connect Telegram first — the background service talks to you through it.")
            }

            // ---- state rows
            Label {
                Layout.fillWidth: true
                visible: page.serviceSupported
                wrapMode: Text.WordWrap
                textSize: Label.Small
                color: theme.palette.normal.backgroundSecondaryText
                text: {
                    if (!page.service) return "";
                    var rows = [];
                    rows.push(i18n.tr("Service: %1").arg(
                        page.service.unit_installed === true
                        ? (page.service.active === "active" ? i18n.tr("running")
                           : (page.service.active || i18n.tr("installed")))
                        : i18n.tr("not installed")));
                    if (page.service.linger !== undefined)
                        rows.push(i18n.tr("Start at boot: %1").arg(
                            page.service.linger === true ? i18n.tr("yes") : i18n.tr("no")));
                    if (page.isUT)
                        rows.push(i18n.tr("Keep-awake: %1").arg(
                            page.service.wakelock_unit_installed === true
                            ? (page.service.wakelock_active === "active"
                               ? i18n.tr("active") : i18n.tr("installed"))
                            : i18n.tr("not installed")));
                    return rows.join("\n");
                }
            }

            Label {
                Layout.fillWidth: true
                visible: page.resultText !== ""
                wrapMode: Text.WordWrap
                color: page.resultIsError ? theme.palette.normal.negative
                                          : theme.palette.normal.positive
                text: page.resultText
            }

            ActivityIndicator {
                Layout.alignment: Qt.AlignHCenter
                running: page.working
                visible: running
            }

            Button {
                Layout.fillWidth: true
                visible: page.serviceSupported
                         && !(page.service && page.service.unit_installed === true)
                enabled: page.telegramReady && !page.working
                color: theme.palette.normal.positive
                text: i18n.tr("Enable background service")
                onClicked: page.enableService()
            }

            Button {
                Layout.fillWidth: true
                visible: page.lingerMissing && page.lingerCommand !== ""
                enabled: !page.working
                color: theme.palette.normal.positive
                text: i18n.tr("Allow start at boot (passcode)")
                onClicked: page.grantLinger()
            }

            Button {
                Layout.fillWidth: true
                visible: page.serviceSupported && page.service
                         && page.service.unit_installed === true
                enabled: !page.working
                text: i18n.tr("Restart Ada")
                onClicked: page.restartService()
            }

            Button {
                Layout.fillWidth: true
                visible: !page.wizardMode && page.serviceSupported && page.service
                         && page.service.unit_installed === true
                enabled: !page.working
                text: i18n.tr("Disable background service")
                onClicked: page.disableService()
            }

            // ---- wakelock card (UT only)
            Label {
                Layout.topMargin: units.gu(2)
                visible: page.isUT
                text: i18n.tr("Keep awake (screen off)")
                textSize: Label.Large
            }
            Label {
                Layout.fillWidth: true
                visible: page.isUT
                wrapMode: Text.WordWrap
                textSize: Label.Small
                color: theme.palette.normal.backgroundSecondaryText
                text: i18n.tr("Phones suspend when the screen turns off, which would freeze Ada. This installs a small system unit that holds a kernel wakelock so Ada keeps running. Needs your device passcode.\n\nNote: system updates (OTA) can delete this unit — if Ada stops answering after an update, re-enable it here (`ada doctor` also detects it).")
            }
            Button {
                Layout.fillWidth: true
                visible: page.isUT
                         && !(page.service && page.service.wakelock_unit_installed === true)
                enabled: !page.working
                         && page.app.api && page.app.api.wakelock_supported === true
                color: theme.palette.normal.positive
                text: page.app.api && page.app.api.wakelock_supported === true
                      ? i18n.tr("Enable keep-awake (passcode)")
                      : i18n.tr("Keep-awake not supported by this kernel")
                onClicked: page.setWakelock(true)
            }
            Button {
                Layout.fillWidth: true
                visible: !page.wizardMode && page.isUT && page.service
                         && page.service.wakelock_unit_installed === true
                enabled: !page.working
                text: i18n.tr("Disable keep-awake (passcode)")
                onClicked: page.setWakelock(false)
            }

            Button {
                Layout.fillWidth: true
                Layout.topMargin: units.gu(2)
                visible: page.wizardMode
                enabled: !page.working
                color: theme.palette.normal.positive
                text: i18n.tr("Finish setup")
                onClicked: page.app.wizardNext()
            }
        }
    }
}
