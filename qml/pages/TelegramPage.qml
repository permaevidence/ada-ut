import QtQuick 2.12
import QtQuick.Layouts 1.12
import Lomiri.Components 1.3
import "../ScannedKeyLogic.js" as ScanLogic

/*
 * Screen 6 — Telegram (UT_APP_PLAN.md §2.4 #6 + Settings). Token probes
 * live (shows the bot's username), chat id must be numeric — the same
 * validation the wizard and setup-api enforce. Optional in the wizard, but
 * required before the background service can be enabled (the service runs
 * `briglia daemon`, whose channel is Telegram).
 */
Page {
    id: page
    property var app
    property bool wizardMode: false

    header: PageHeader {
        title: page.wizardMode ? i18n.tr("Step 4 · Telegram") : i18n.tr("Telegram")
    }

    readonly property var telegram: app.api && app.api.telegram ? app.api.telegram : null

    property bool working: false
    property string resultText: ""
    property bool resultIsError: false

    function fail(text) { working = false; resultIsError = true; resultText = text; }

    // Exact-value bundle ownership (ScannedKeyLogic.js) for both fields:
    // each follows the bundle only while it holds the exact injected
    // value; hand-typed/edited values are never touched.
    property string tokenInjected: ""
    property string chatInjected: ""

    function syncScanned() {
        var r = ScanLogic.sync(tokenField.text, tokenInjected,
                               app.scannedKeys ? app.scannedKeys.telegram_token : "");
        if (tokenField.text !== r.text) tokenField.text = r.text;
        tokenInjected = r.injected;
        r = ScanLogic.sync(chatIdField.text, chatInjected,
                           app.scannedKeys ? app.scannedKeys.telegram_chat_id : "");
        if (chatIdField.text !== r.text) chatIdField.text = r.text;
        chatInjected = r.injected;
    }
    function prefill() { syncScanned(); }
    Component.onCompleted: prefill()
    Connections {
        target: page.app
        onScannedKeysChanged: page.syncScanned()
    }

    // A scanned/typed token that was never committed — same fat-thumb trap
    // as the key cards: the wizard clears bundle values when it ends.
    readonly property bool unsaved: tokenField.text.trim() !== ""

    // done(ok) is optional — the wizard's Continue auto-commit uses it.
    function saveTelegram(done) {
        var token = tokenField.text.trim();
        var chatId = chatIdField.text.trim();
        if (token === "") {
            fail(i18n.tr("Paste the bot token first."));
            if (done) done(false);
            return;
        }
        if (chatId === "" || isNaN(parseInt(chatId, 10)) || !/^-?\d+$/.test(chatId)) {
            fail(i18n.tr("The chat ID must be a number — message @userinfobot on Telegram to get yours."));
            if (done) done(false);
            return;
        }
        working = true; resultIsError = false;
        resultText = i18n.tr("Checking the bot token…");
        app.apiProbe({kind: "telegram", token: token}, function(probe) {
            if (!probe || probe.ok !== true) {
                page.fail(i18n.tr("Token check failed: %1").arg(page.app.describeError(probe)));
                if (done) done(false);
                return;
            }
            if (probe.bot_username)
                page.resultText = i18n.tr("Bot @%1 found — saving…").arg(probe.bot_username);
            page.app.apiApply({telegram: {token: token, chat_id: chatId}}, function(result) {
                page.working = false;
                if (!result || result.ok !== true) {
                    page.fail(page.app.describeError(result));
                    if (done) done(false);
                    return;
                }
                page.resultText = probe.bot_username
                    ? i18n.tr("Saved — Briglia will talk to you through @%1.").arg(probe.bot_username)
                    : i18n.tr("Saved.");
                tokenField.text = "";
                page.app.consumeScannedKeys(["telegram_token", "telegram_chat_id"]);
                restartHint.lastResult = result;
                page.app.refresh();
                if (done) done(true);
            });
        });
    }

    function removeTelegram() {
        working = true; resultIsError = false; resultText = "";
        app.apiApply({telegram: {remove: true}}, function(result) {
            page.working = false;
            if (!result || result.ok !== true) {
                page.fail(page.app.describeError(result));
                return;
            }
            page.resultText = i18n.tr("Telegram disconnected.");
            tokenField.text = "";
            chatIdField.text = "";
            page.app.consumeScannedKeys(["telegram_token", "telegram_chat_id"]);
            restartHint.lastResult = result;
            page.app.refresh();
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
                text: i18n.tr("Telegram is how you talk to Briglia from anywhere. Create a bot with @BotFather (one message, free), paste its token here, and get your numeric chat ID from @userinfobot.")
            }

            Label {
                Layout.fillWidth: true
                visible: page.telegram !== null && page.telegram.configured === true
                wrapMode: Text.WordWrap
                textSize: Label.Small
                color: theme.palette.normal.backgroundSecondaryText
                text: i18n.tr("Current: token %1 · chat ID %2")
                      .arg(page.telegram && page.telegram.masked_token ? page.telegram.masked_token : "—")
                      .arg(page.telegram && page.telegram.chat_id ? page.telegram.chat_id : "—")
            }

            RowLayout {
                Layout.fillWidth: true
                TextField {
                    id: tokenField
                    Layout.fillWidth: true
                    echoMode: TextInput.Password
                    inputMethodHints: Qt.ImhNoPredictiveText | Qt.ImhNoAutoUppercase | Qt.ImhSensitiveData
                    placeholderText: page.telegram && page.telegram.configured === true
                                     ? i18n.tr("New bot token — replaces the saved one")
                                     : i18n.tr("Bot token from @BotFather")
                }
                Button {
                    text: i18n.tr("Scan")
                    enabled: !page.working
                    onClicked: page.app.openScan("single", function(res) {
                        if (res && res.kind === "text") tokenField.text = res.text;
                    })
                }
            }

            TextField {
                id: chatIdField
                Layout.fillWidth: true
                inputMethodHints: Qt.ImhFormattedNumbersOnly | Qt.ImhNoPredictiveText
                placeholderText: i18n.tr("Your numeric chat ID (from @userinfobot)")
                Component.onCompleted: {
                    if (page.telegram && page.telegram.chat_id)
                        text = page.telegram.chat_id;
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
                enabled: !page.working
                color: theme.palette.normal.positive
                text: i18n.tr("Verify & save")
                onClicked: page.saveTelegram()
            }

            Button {
                Layout.fillWidth: true
                visible: !page.wizardMode && page.telegram !== null
                         && page.telegram.configured === true
                enabled: !page.working
                text: i18n.tr("Disconnect Telegram")
                onClicked: page.removeTelegram()
            }

            RestartHint {
                id: restartHint
                Layout.fillWidth: true
                app: page.app
            }

            Label {
                Layout.fillWidth: true
                visible: page.unsaved && !page.working
                wrapMode: Text.WordWrap
                textSize: Label.Small
                color: "#c7662a"  // warning orange — literal: no unverified toolkit singleton
                text: page.wizardMode
                      ? i18n.tr("Bot token not saved yet — Continue below verifies and saves it.")
                      : i18n.tr("Bot token not saved yet — tap Verify & save to store it.")
            }

            Button {
                Layout.fillWidth: true
                visible: page.wizardMode
                enabled: !page.working
                color: page.unsaved || (page.telegram && page.telegram.configured === true)
                       ? theme.palette.normal.positive : theme.palette.normal.foreground
                text: page.unsaved
                      ? i18n.tr("Verify, save & continue")
                      : (page.telegram && page.telegram.configured === true
                         ? i18n.tr("Continue") : i18n.tr("Skip for now"))
                onClicked: {
                    if (page.unsaved)
                        page.saveTelegram(function(ok) { if (ok) page.app.wizardNext(); });
                    else
                        page.app.wizardNext();
                }
            }
        }
    }
}
