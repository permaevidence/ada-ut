import QtQuick 2.12
import QtQuick.Layouts 1.12
import Lomiri.Components 1.3
import Lomiri.Components.Popups 1.3

/*
 * Identity migration consent (rename plan §4.2/§5): a phone that ran the
 * CLI under its previous identity ("ada") keeps everything — configuration,
 * memory, watchers, Telegram, the background service — but ONLY after the
 * user says so here. The move itself is the CLI's own journaled engine,
 * reached through `briglia setup-api migrate` (briglia_bridge.migrate);
 * this page never touches a file. The CLI's status block is the authority
 * for what is shown: `migration.needed` (an old install is present),
 * `migration.conflict` (old AND new directories coexist — resolved by hand,
 * the engine refuses to guess), `migration.journal_state` (an earlier run
 * was interrupted — recover, or roll back before the commit point).
 *
 * Step 2, Ubuntu Touch only: the keep-awake SYSTEM unit is root-owned, so
 * an unprivileged migration records it and leaves the swap to this page's
 * passcode dialog — Briglia's unit is installed first, the legacy unit
 * removed after (deliberate overlap: the phone never gets a chance to
 * suspend in between). Declining leaves the legacy unit in place, which
 * still keeps the phone awake; the Dashboard offers the swap again later.
 *
 * mode: "migrate" (default) — full flow; "wakelock" — only the swap step,
 * for a phone that migrated earlier and declined (or failed) the swap.
 */
Page {
    id: page
    property var app
    property string mode: "migrate"

    header: PageHeader {
        title: page.mode === "wakelock" ? i18n.tr("Keep-awake migration")
                                        : i18n.tr("Move Ada to Briglia")
    }

    readonly property var migration: app.api && app.api.migration ? app.api.migration : null
    readonly property var legacy: app.detectInfo && app.detectInfo.legacy ? app.detectInfo.legacy : null
    readonly property bool needed: migration !== null && migration.needed === true
    readonly property bool conflict: migration !== null && migration.conflict === true
    readonly property string journalState: migration && migration.journal_state ? String(migration.journal_state) : ""
    readonly property bool interrupted: journalState !== ""
    // Rollback is honored by the engine only before its commit point; the
    // engine is the authority (it refuses otherwise), the button just
    // hides the option for states where the answer is known to be no.
    readonly property bool rollbackOffered: interrupted
        && ["prepared", "moved", "fixups"].indexOf(journalState) !== -1
    readonly property bool legacyWakelock: legacy !== null && legacy.wakelock_unit === true

    // phase: "consent" → "running" → "done" | "failed" → (UT) "wakelock" → "finished"
    property string phase: page.mode === "wakelock" ? "wakelock" : "consent"
    property bool working: false
    property string message: ""
    property bool messageIsError: false
    property var logLines: []

    function fail(text) { working = false; messageIsError = true; message = text; }

    function describeRoots() {
        if (!migration) return "";
        var old = migration.old_roots_present || [];
        return old.join("\n");
    }

    // ---------------------------------------------------- step 1: migrate
    function runMigration(rollback) {
        working = true; messageIsError = false; logLines = [];
        message = rollback ? i18n.tr("Rolling back the interrupted migration…")
                           : (page.interrupted ? i18n.tr("Recovering the interrupted migration…")
                                               : i18n.tr("Migrating… this takes a minute: the old service is stopped, the data is moved, Briglia is health-checked, then started."));
        phase = "running";
        app.pyCall("migrate", [rollback === true], function(result) {
            page.working = false;
            page.logLines = result && result.log ? result.log : [];
            page.app.refresh(function() {
                if (!result || result.ok !== true) {
                    page.phase = "failed";
                    page.messageIsError = true;
                    page.message = page.app.describeError(result);
                    return;
                }
                page.messageIsError = false;
                if (result.outcome === "rolled_back") {
                    page.phase = "done";
                    page.message = i18n.tr("Rolled back — the previous installation is back in place. Briglia is not set up on this phone.");
                    return;
                }
                if (result.outcome === "nothing_to_do") {
                    page.phase = "done";
                    page.message = i18n.tr("Nothing to migrate — this phone is already on Briglia.");
                    return;
                }
                var notes = result.notes || [];
                page.message = i18n.tr("Migrated. Your assistant answers to Bree now unless you had given it a custom name.")
                    + (notes.length ? "\n\n" + notes.join("\n") : "");
                // Step 2 only where a legacy keep-awake unit remains.
                page.phase = page.legacyWakelock ? "wakelock" : "done";
            });
        });
    }

    // ---------------------------------------------------- step 2: wakelock swap
    property var pendingPrivileged: null
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

    function swapWakelock() {
        working = true; messageIsError = false;
        askPasscode(i18n.tr("Installs Briglia's keep-awake unit, then removes the old one (%1). The phone stays awake throughout. Briefly remounts the system partition read-write.")
                        .arg(page.legacy ? page.legacy.wakelock_unit_name : i18n.tr("the old unit")),
                    function(passcode) {
            page.message = i18n.tr("Swapping the keep-awake unit…");
            page.app.pyCall("swap_legacy_wakelock", [passcode], function(result) {
                page.app.refresh(function() {
                    if (!result || result.ok !== true) {
                        page.fail(result && result.error ? result.error : i18n.tr("sudo failed"));
                        return;
                    }
                    page.working = false;
                    page.messageIsError = false;
                    page.message = i18n.tr("Keep-awake swapped — Briglia stays reachable with the screen off.");
                    page.phase = "finished";
                });
            });
        });
    }

    function skipWakelock() {
        // Honest: nothing is lost by declining — the legacy unit keeps the
        // phone awake — but the swap stays pending and the Dashboard says so.
        message = i18n.tr("Keep-awake left on the old unit for now. The Dashboard offers the swap whenever you're ready.");
        messageIsError = false;
        phase = "finished";
    }

    function leave() {
        app.refresh(function() {
            page.app.popPage();
            if (page.app.api && page.app.api.migration && page.app.api.migration.needed === true)
                return;  // still pending (declined / failed): the boot page shows the state
            if (page.app.api && page.app.api.setup && page.app.api.setup.complete === true)
                page.app.gotoShell();
            else
                page.app.startWizard();
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

            // ---------------------------------------------- consent
            Label {
                Layout.fillWidth: true
                visible: page.phase === "consent"
                wrapMode: Text.WordWrap
                text: {
                    if (page.conflict)
                        return i18n.tr("An Ada installation AND Briglia directories both exist on this phone. Briglia will not silently ignore the Ada data, and the migration refuses to move onto pre-existing Briglia directories.\n\nResolve by hand in the Terminal app, then check again: move the Briglia directories aside (e.g. `mv <dir> <dir>.bak`) if they are stray or from a fresh setup you don't need — the Ada data then migrates; or remove the old Ada directories if you no longer need that data.\n\nAda directories: %1\nBriglia directories: %2")
                            .arg((page.migration.old_roots_present || []).join(", "))
                            .arg((page.migration.new_roots_present || []).join(", "));
                    if (page.interrupted)
                        return i18n.tr("An earlier migration was interrupted (journal state: %1). Nothing is changed until you choose: Recover completes it; Roll back returns the phone to Ada (possible only before the commit point — the migration itself decides and says so).").arg(page.journalState);
                    if (page.needed)
                        return i18n.tr("Ada CLI's data was found on this phone:\n%1\n\nMoving it to Briglia keeps everything — configuration and keys, memory and archives, watchers, Telegram, the background service. Nothing is deleted: every step is journaled and the old installation is restored if the migration cannot complete. The old `ada` command keeps working as an alias.\n\nYour assistant will answer to \"Bree\" from now on, unless you had given it a custom name.").arg(page.describeRoots());
                    return i18n.tr("Nothing to migrate — this phone is already on Briglia.");
                }
            }

            Button {
                Layout.fillWidth: true
                visible: page.phase === "consent" && page.needed && !page.conflict && !page.interrupted
                enabled: !page.working
                color: theme.palette.normal.positive
                text: i18n.tr("Migrate now")
                onClicked: page.runMigration(false)
            }
            Button {
                Layout.fillWidth: true
                visible: page.phase === "consent" && page.interrupted
                enabled: !page.working
                color: theme.palette.normal.positive
                text: i18n.tr("Recover")
                onClicked: page.runMigration(false)
            }
            Button {
                Layout.fillWidth: true
                visible: page.phase === "consent" && page.rollbackOffered
                enabled: !page.working
                text: i18n.tr("Roll back to Ada")
                onClicked: page.runMigration(true)
            }
            Button {
                Layout.fillWidth: true
                visible: page.phase === "consent" && page.conflict
                enabled: !page.working
                color: theme.palette.normal.positive
                text: i18n.tr("Check again")
                onClicked: {
                    page.working = true;
                    page.app.refresh(function() { page.working = false; });
                }
            }
            Button {
                Layout.fillWidth: true
                visible: page.phase === "consent"
                enabled: !page.working
                text: page.needed ? i18n.tr("Not now") : i18n.tr("Back")
                onClicked: page.app.popPage()
            }

            // ---------------------------------------------- progress / result
            ActivityIndicator {
                Layout.alignment: Qt.AlignHCenter
                running: page.working
                visible: running
            }
            Label {
                Layout.fillWidth: true
                visible: page.message !== ""
                wrapMode: Text.WordWrap
                color: page.messageIsError ? theme.palette.normal.negative
                                           : theme.palette.normal.backgroundSecondaryText
                text: page.message
            }
            LomiriShape {
                Layout.fillWidth: true
                visible: page.logLines.length > 0
                implicitHeight: logLabel.height + units.gu(2)
                aspect: LomiriShape.Flat
                backgroundColor: theme.palette.normal.foreground
                Label {
                    id: logLabel
                    anchors { left: parent.left; right: parent.right; top: parent.top; margins: units.gu(1) }
                    wrapMode: Text.WrapAnywhere
                    textSize: Label.XSmall
                    font.family: "Ubuntu Mono"
                    text: page.logLines.join("\n")
                }
            }

            Button {
                Layout.fillWidth: true
                visible: page.phase === "failed"
                enabled: !page.working
                color: theme.palette.normal.positive
                text: i18n.tr("Check again")
                onClicked: {
                    page.working = true;
                    page.app.refresh(function() {
                        page.working = false;
                        page.phase = "consent";
                        page.message = "";
                        page.logLines = [];
                    });
                }
            }
            Button {
                Layout.fillWidth: true
                visible: page.phase === "failed"
                text: i18n.tr("Back")
                onClicked: page.app.popPage()
            }

            // ---------------------------------------------- step 2: wakelock
            Label {
                Layout.fillWidth: true
                visible: page.phase === "wakelock"
                wrapMode: Text.WordWrap
                textSize: Label.Large
                text: i18n.tr("One more step: keep-awake")
            }
            Label {
                Layout.fillWidth: true
                visible: page.phase === "wakelock"
                wrapMode: Text.WordWrap
                text: page.legacyWakelock
                    ? i18n.tr("The phone's keep-awake unit still belongs to the old installation (%1). It is a system unit, so swapping it needs your device passcode: Briglia's unit is installed first, the old one removed after — the phone stays awake throughout.")
                          .arg(page.legacy.wakelock_unit_name)
                    : i18n.tr("No old keep-awake unit is installed — nothing to swap.")
            }
            Button {
                Layout.fillWidth: true
                visible: page.phase === "wakelock" && page.legacyWakelock
                enabled: !page.working
                color: theme.palette.normal.positive
                text: i18n.tr("Swap keep-awake now")
                onClicked: page.swapWakelock()
            }
            Button {
                Layout.fillWidth: true
                visible: page.phase === "wakelock"
                enabled: !page.working
                text: page.legacyWakelock ? i18n.tr("Later") : i18n.tr("Continue")
                onClicked: page.legacyWakelock ? page.skipWakelock() : page.leave()
            }

            // ---------------------------------------------- done
            Button {
                Layout.fillWidth: true
                visible: page.phase === "done" || page.phase === "finished"
                enabled: !page.working
                color: theme.palette.normal.positive
                text: i18n.tr("Continue")
                onClicked: page.leave()
            }
        }
    }
}
