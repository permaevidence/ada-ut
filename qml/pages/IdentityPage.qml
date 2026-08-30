import QtQuick 2.12
import QtQuick.Layouts 1.12
import Lomiri.Components 1.3
import "../ScannedKeyLogic.js" as ScanLogic

/*
 * Screen 5 — "You": name + email/calendar provider (UT_APP_PLAN.md §2.4 #5
 * and the matching Settings slice). Name is required to continue the
 * wizard; email is optional (None / AgentMail / Google Workspace with the
 * user's own OAuth client). AgentMail keys probe live and show the inbox
 * address; provider "none" in Settings can optionally delete the stored
 * email credentials (the setup-api remove_credentials path).
 */
Page {
    id: page
    property var app
    property bool wizardMode: false

    header: PageHeader {
        title: page.wizardMode ? i18n.tr("Step 3 · About you") : i18n.tr("Name & email")
    }

    readonly property var identity: app.api && app.api.identity ? app.api.identity : null
    readonly property var email: app.api && app.api.email_calendar ? app.api.email_calendar : null
    readonly property string storedName: identity && identity.user_name ? identity.user_name : ""

    property var providerIds: ["none", "agentmail", "gws"]
    readonly property string providerId: providerIds[providerSelector.selectedIndex]

    property bool working: false
    property string nameResult: ""
    property bool nameError: false
    property string emailResult: ""
    property bool emailError: false

    // Exact-value bundle ownership (ScannedKeyLogic.js, same contract as
    // the other pages): the field follows the bundle only while it holds
    // the exact injected value; hand-typed/edited text is never touched.
    property string agentmailInjected: ""

    function syncScanned() {
        var r = ScanLogic.sync(agentmailKeyField.text, agentmailInjected,
                               app.scannedKeys ? app.scannedKeys.agentmail : "");
        if (agentmailKeyField.text !== r.text) agentmailKeyField.text = r.text;
        agentmailInjected = r.injected;
    }
    function prefillAgentmail() { syncScanned(); }
    Connections {
        target: page.app
        onScannedKeysChanged: page.syncScanned()
    }

    // Fat-thumb protection (field lesson, 2026-08-29): filled-but-unsaved
    // values must not die silently when the wizard moves on — Continue
    // commits them itself (name first, then email) and only advances when
    // everything stored; a failure keeps you here with its error visible.
    readonly property bool namePending:
        nameField.text.trim() !== "" && nameField.text.trim() !== storedName
    readonly property bool emailPending:
        (providerId === "agentmail" && agentmailKeyField.text.trim() !== "")
        || (providerId === "gws" && (gwsIdField.text.trim() !== ""
                                     || gwsSecretField.text.trim() !== ""))

    // done(ok) is optional on both savers — the Continue auto-commit uses it.
    function saveName(done) {
        var name = nameField.text.trim() !== "" ? nameField.text.trim() : storedName;
        if (name === "") {
            nameError = true;
            nameResult = i18n.tr("A name is required.");
            if (done) done(false);
            return;
        }
        working = true; nameError = false; nameResult = "";
        app.apiApply({identity: {user_name: name}}, function(result) {
            page.working = false;
            if (!result || result.ok !== true) {
                page.nameError = true;
                page.nameResult = page.app.describeError(result);
                if (done) done(false);
                return;
            }
            page.nameResult = i18n.tr("Saved.");
            nameField.text = "";
            restartHint.lastResult = result;
            page.app.refresh();
            if (done) done(true);
        });
    }

    function commitAndContinue() {
        var afterName = function() {
            if (page.emailPending)
                page.saveEmail(function(ok) { if (ok) page.app.wizardNext(); });
            else
                page.app.wizardNext();
        };
        if (namePending)
            saveName(function(ok) { if (ok) afterName(); });
        else
            afterName();
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

            // ---------------------------------------------------- name
            Label { text: i18n.tr("Your name"); textSize: Label.Large }
            Label {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                textSize: Label.Small
                color: theme.palette.normal.backgroundSecondaryText
                text: i18n.tr("Ada uses it to address you. The assistant's own name stays \"Ada\".")
            }
            TextField {
                id: nameField
                Layout.fillWidth: true
                placeholderText: page.storedName !== ""
                                 ? i18n.tr("Name — blank keeps \"%1\"").arg(page.storedName)
                                 : i18n.tr("e.g. Sofia")
            }
            Label {
                Layout.fillWidth: true
                visible: page.nameResult !== ""
                wrapMode: Text.WordWrap
                textSize: Label.Small
                color: page.nameError ? theme.palette.normal.negative
                                      : theme.palette.normal.positive
                text: page.nameResult
            }
            Button {
                Layout.fillWidth: true
                enabled: !page.working
                color: theme.palette.normal.positive
                text: i18n.tr("Save name")
                onClicked: page.saveName()
            }

            // ---------------------------------------------------- email
            Label {
                Layout.topMargin: units.gu(2)
                text: i18n.tr("Email & calendar")
                textSize: Label.Large
            }
            Label {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                textSize: Label.Small
                color: theme.palette.normal.backgroundSecondaryText
                text: {
                    var current = page.email ? page.email.provider : "none";
                    var line = i18n.tr("Optional: give Ada an inbox and calendar. AgentMail is a dedicated agent inbox (recommended); Google Workspace needs your own Google OAuth client.");
                    if (current === "agentmail" && page.email.agentmail_inbox)
                        line += "\n" + i18n.tr("Current: AgentMail (%1)").arg(page.email.agentmail_inbox);
                    else if (current !== "none")
                        line += "\n" + i18n.tr("Current: %1").arg(current);
                    return line;
                }
            }
            OptionSelector {
                id: providerSelector
                Layout.fillWidth: true
                model: [i18n.tr("None"), i18n.tr("AgentMail (recommended)"),
                        i18n.tr("Google Workspace")]
                selectedIndex: {
                    var current = page.email ? page.email.provider : "none";
                    var i = page.providerIds.indexOf(current);
                    return i >= 0 ? i : 0;
                }
                onSelectedIndexChanged: { page.emailResult = ""; page.emailError = false; }
            }

            RowLayout {
                Layout.fillWidth: true
                visible: page.providerId === "agentmail"
                TextField {
                    id: agentmailKeyField
                    Layout.fillWidth: true
                    echoMode: TextInput.Password
                    inputMethodHints: Qt.ImhNoPredictiveText | Qt.ImhNoAutoUppercase | Qt.ImhSensitiveData
                    placeholderText: page.app.api && page.app.api.keys
                                     && page.app.api.keys.agentmail
                                     && page.app.api.keys.agentmail.set === true
                                     ? i18n.tr("AgentMail key — blank keeps the saved key")
                                     : i18n.tr("AgentMail API key")
                    Component.onCompleted: page.prefillAgentmail()
                }
                Button {
                    text: i18n.tr("Scan")
                    enabled: !page.working
                    onClicked: page.app.openScan("single", function(res) {
                        if (res && res.kind === "text") agentmailKeyField.text = res.text;
                    })
                }
            }

            TextField {
                id: gwsIdField
                Layout.fillWidth: true
                visible: page.providerId === "gws"
                inputMethodHints: Qt.ImhNoPredictiveText | Qt.ImhNoAutoUppercase
                placeholderText: page.email && page.email.gws_client_secret_present === true
                                 ? i18n.tr("Google OAuth client ID — blank keeps the saved one")
                                 : i18n.tr("Google OAuth client ID")
            }
            TextField {
                id: gwsSecretField
                Layout.fillWidth: true
                visible: page.providerId === "gws"
                echoMode: TextInput.Password
                inputMethodHints: Qt.ImhNoPredictiveText | Qt.ImhNoAutoUppercase | Qt.ImhSensitiveData
                placeholderText: i18n.tr("Google OAuth client secret")
            }
            Label {
                Layout.fillWidth: true
                visible: page.providerId === "gws"
                wrapMode: Text.WordWrap
                textSize: Label.Small
                color: theme.palette.normal.backgroundSecondaryText
                text: i18n.tr("After saving, run `gws auth login` once in the Terminal to authorize — Google's login needs a browser round-trip the app cannot do for you yet.")
            }

            RowLayout {
                Layout.fillWidth: true
                visible: !page.wizardMode && page.providerId === "none"
                         && page.email && page.email.provider !== "none"
                Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    textSize: Label.Small
                    text: i18n.tr("Also delete stored email credentials (AgentMail key, Google client + tokens)")
                }
                CheckBox { id: removeCredentialsBox }
            }

            Label {
                Layout.fillWidth: true
                visible: page.emailResult !== ""
                wrapMode: Text.WordWrap
                textSize: Label.Small
                color: page.emailError ? theme.palette.normal.negative
                                       : theme.palette.normal.positive
                text: page.emailResult
            }

            Label {
                Layout.fillWidth: true
                visible: page.emailPending && !page.working
                wrapMode: Text.WordWrap
                textSize: Label.Small
                color: "#c7662a"  // warning orange — literal: no unverified toolkit singleton
                text: page.wizardMode
                      ? i18n.tr("Not saved yet — Continue below verifies and saves it.")
                      : i18n.tr("Not saved yet — tap Save email settings to store it.")
            }
            Button {
                Layout.fillWidth: true
                enabled: !page.working
                color: theme.palette.normal.positive
                text: i18n.tr("Save email settings")
                onClicked: page.saveEmail()
            }

            ActivityIndicator {
                Layout.alignment: Qt.AlignHCenter
                running: page.working
                visible: running
            }

            RestartHint {
                id: restartHint
                Layout.fillWidth: true
                app: page.app
            }

            Button {
                Layout.fillWidth: true
                visible: page.wizardMode
                enabled: (page.storedName !== "" || page.namePending) && !page.working
                color: theme.palette.normal.positive
                text: page.namePending || page.emailPending
                      ? i18n.tr("Verify, save & continue")
                      : (page.storedName !== "" ? i18n.tr("Continue")
                                                : i18n.tr("Save your name to continue"))
                onClicked: page.commitAndContinue()
            }
        }
    }

    function saveEmail(done) {
        var section = {provider: providerId};
        if (providerId === "agentmail") {
            if (agentmailKeyField.text.trim() !== "")
                section.api_key = agentmailKeyField.text.trim();
            section.install_cli = true;
        } else if (providerId === "gws") {
            if (gwsIdField.text.trim() !== "") section.gws_client_id = gwsIdField.text.trim();
            if (gwsSecretField.text.trim() !== "") section.gws_client_secret = gwsSecretField.text.trim();
            section.install_cli = true;
        } else if (!wizardMode && removeCredentialsBox.checked) {
            section.remove_credentials = true;
        }
        working = true; emailError = false;
        emailResult = providerId === "agentmail"
                      ? i18n.tr("Checking the key and setting up…") : "";
        var finish = function(result) {
            page.working = false;
            if (!result || result.ok !== true) {
                page.emailError = true;
                page.emailResult = page.app.describeError(result);
                if (done) done(false);
                return;
            }
            var note = i18n.tr("Saved.");
            if (result.warnings && result.warnings.length > 0)
                note += "\n" + result.warnings.join("\n");
            page.emailResult = note;
            agentmailKeyField.text = ""; gwsSecretField.text = ""; gwsIdField.text = "";
            // Consume on agentmail save AND on switching away/removing
            // credentials — either way the scanned key must not resurface.
            page.app.consumeScannedKeys(["agentmail"]);
            restartHint.lastResult = result;
            page.app.refresh();
            if (done) done(true);
        };
        if (providerId === "agentmail" && agentmailKeyField.text.trim() !== "") {
            // Probe first so a bad key gets a clear verdict (apply treats an
            // unreachable probe as a warning, not a failure).
            app.apiProbe({kind: "agentmail", api_key: agentmailKeyField.text.trim()},
                         function(probe) {
                if (!probe || probe.ok !== true) {
                    page.working = false;
                    page.emailError = true;
                    page.emailResult = i18n.tr("Key check failed: %1").arg(page.app.describeError(probe));
                    if (done) done(false);
                    return;
                }
                if (probe.inboxes && probe.inboxes.length > 0)
                    page.emailResult = i18n.tr("Inbox: %1 — saving…").arg(probe.inboxes[0]);
                page.app.apiApply({email_calendar: section}, finish);
            });
        } else {
            app.apiApply({email_calendar: section}, finish);
        }
    }
}
