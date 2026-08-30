import QtQuick 2.12
import QtQuick.Layouts 1.12
import Lomiri.Components 1.3
import "../ScannedKeyLogic.js" as ScanLogic

/*
 * Screen 4 — service keys (UT_APP_PLAN.md §2.4 #4 + the key half of
 * Settings #9). Three independent cards: OpenAI (research/voice/images/OCR
 * fan-out), Serper (web search), Jina (page reading). Each verifies with a
 * live probe before saving; every key is optional (skippable in the wizard,
 * removable in Settings).
 */
Page {
    id: page
    property var app
    property bool wizardMode: false

    header: PageHeader {
        title: page.wizardMode ? i18n.tr("Step 2 · Service keys") : i18n.tr("API keys")
    }

    readonly property var keys: app.api && app.api.keys ? app.api.keys : null

    // Fat-thumb protection (field lesson, 2026-08-29): a scanned bundle
    // pre-fills all three fields, and it is far too easy to tap Continue
    // without pressing each card's "Verify & save" — the wizard then ends,
    // clears the bundle, and the keys are simply gone. Continue therefore
    // commits every filled field itself (probe + save, one card at a time)
    // and only advances when all of them stored successfully; a failed card
    // shows its error and keeps you on this page.
    property bool committing: false
    readonly property int pendingCount:
        (openaiCard.item && openaiCard.item.unsaved ? 1 : 0)
        + (serperCard.item && serperCard.item.unsaved ? 1 : 0)
        + (jinaCard.item && jinaCard.item.unsaved ? 1 : 0)

    function commitAndContinue() {
        var pending = [];
        var loaders = [openaiCard, serperCard, jinaCard];
        for (var i = 0; i < loaders.length; i++)
            if (loaders[i].item && loaders[i].item.unsaved)
                pending.push(loaders[i].item);
        if (pending.length === 0) { app.wizardNext(); return; }
        committing = true;
        var step = function(idx) {
            if (idx >= pending.length) {
                page.committing = false;
                page.app.wizardNext();
                return;
            }
            pending[idx].saveKey(function(ok) {
                if (!ok) { page.committing = false; return; }
                step(idx + 1);
            });
        };
        step(0);
    }

    // One reusable card; kind doubles as the probe kind and apply section.
    Component {
        id: keyCard
        ColumnLayout {
            id: card
            property string kind
            property string title
            property string blurb
            property bool working: false
            property string resultText: ""
            property bool resultIsError: false

            readonly property var status: page.keys && page.keys[card.kind] ? page.keys[card.kind] : null
            spacing: units.gu(0.8)

            // A filled field that was never committed (fat-thumb trap: scan
            // the bundle, tap Continue, keys silently die with the wizard).
            readonly property bool unsaved: field.text.trim() !== ""

            function fail(text) { working = false; resultIsError = true; resultText = text; }

            // done(ok) is optional — the wizard's auto-commit chain uses it;
            // the per-card "Verify & save" button doesn't.
            function saveKey(done) {
                var key = field.text.trim();
                if (key === "") {
                    fail(i18n.tr("Paste a key first."));
                    if (done) done(false);
                    return;
                }
                working = true; resultIsError = false;
                resultText = i18n.tr("Checking the key…");
                page.app.apiProbe({kind: card.kind, api_key: key}, function(probe) {
                    if (!probe || probe.ok !== true) {
                        card.fail(i18n.tr("Key check failed: %1").arg(page.app.describeError(probe)));
                        if (done) done(false);
                        return;
                    }
                    var request = {};
                    request[card.kind] = {api_key: key};
                    page.app.apiApply(request, function(result) {
                        card.working = false;
                        if (!result || result.ok !== true) {
                            card.fail(page.app.describeError(result));
                            if (done) done(false);
                            return;
                        }
                        card.resultText = i18n.tr("Saved.");
                        field.text = "";
                        page.app.consumeScannedKeys([card.kind]);
                        restartHint.lastResult = result;
                        page.app.refresh();
                        if (done) done(true);
                    });
                });
            }

            function removeKey() {
                working = true; resultIsError = false; resultText = "";
                var request = {};
                request[card.kind] = {remove: true};
                page.app.apiApply(request, function(result) {
                    card.working = false;
                    if (!result || result.ok !== true) {
                        card.fail(page.app.describeError(result));
                        return;
                    }
                    card.resultText = i18n.tr("Key removed.");
                    field.text = "";
                    page.app.consumeScannedKeys([card.kind]);
                    restartHint.lastResult = result;
                    page.app.refresh();
                });
            }

            // Pre-fill from a scanned key bundle; the value still goes
            // through the same probe-then-apply as a typed key.
            // Exact-value ownership (ScannedKeyLogic.js): the field
            // follows the bundle only while it holds the exact injected
            // value; hand-typed/edited text is never touched.
            property string fieldInjected: ""
            function syncScanned() {
                if (kind === "") return;
                var r = ScanLogic.sync(field.text, fieldInjected,
                                       page.app.scannedKeys ? page.app.scannedKeys[kind] : "");
                if (field.text !== r.text) field.text = r.text;
                fieldInjected = r.injected;
            }
            function prefill() { syncScanned(); }
            onKindChanged: prefill()
            Connections {
                target: page.app
                onScannedKeysChanged: card.syncScanned()
            }

            Label { text: card.title; textSize: Label.Large }
            Label {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                textSize: Label.Small
                color: theme.palette.normal.backgroundSecondaryText
                text: card.blurb + (card.status && card.status.set === true
                      ? "\n" + i18n.tr("Current key: %1").arg(card.status.masked) : "")
            }
            RowLayout {
                Layout.fillWidth: true
                TextField {
                    id: field
                    Layout.fillWidth: true
                    echoMode: TextInput.Password
                    inputMethodHints: Qt.ImhNoPredictiveText | Qt.ImhNoAutoUppercase | Qt.ImhSensitiveData
                    placeholderText: card.status && card.status.set === true
                                     ? i18n.tr("New key — replaces the saved one")
                                     : i18n.tr("API key")
                }
                Button {
                    text: i18n.tr("Scan")
                    enabled: !card.working
                    onClicked: page.app.openScan("single", function(res) {
                        if (res && res.kind === "text") field.text = res.text;
                    })
                }
            }
            Label {
                Layout.fillWidth: true
                visible: card.unsaved && !card.working
                wrapMode: Text.WordWrap
                textSize: Label.Small
                color: "#c7662a"  // warning orange — literal: no unverified toolkit singleton
                text: page.wizardMode
                      ? i18n.tr("Not saved yet — Continue below verifies and saves it.")
                      : i18n.tr("Not saved yet — tap Verify & save to store it.")
            }
            Label {
                Layout.fillWidth: true
                visible: card.resultText !== ""
                wrapMode: Text.WordWrap
                textSize: Label.Small
                color: card.resultIsError ? theme.palette.normal.negative
                                          : theme.palette.normal.positive
                text: card.resultText
            }
            RowLayout {
                Layout.fillWidth: true
                Button {
                    Layout.fillWidth: true
                    enabled: !card.working
                    color: theme.palette.normal.positive
                    text: i18n.tr("Verify & save")
                    onClicked: card.saveKey()
                }
                Button {
                    Layout.fillWidth: true
                    visible: !page.wizardMode && card.status && card.status.set === true
                    enabled: !card.working
                    text: i18n.tr("Remove")
                    onClicked: card.removeKey()
                }
            }
        }
    }

    Flickable {
        anchors { top: page.header.bottom; left: parent.left; right: parent.right; bottom: parent.bottom }
        contentHeight: column.height + units.gu(6)
        clip: true

        ColumnLayout {
            id: column
            anchors { top: parent.top; topMargin: units.gu(2); horizontalCenter: parent.horizontalCenter }
            width: Math.min(parent.width - units.gu(4), units.gu(50))
            spacing: units.gu(3)

            Label {
                Layout.fillWidth: true
                visible: page.wizardMode
                wrapMode: Text.WordWrap
                color: theme.palette.normal.backgroundSecondaryText
                text: i18n.tr("These unlock web research and media features. All three are optional — you can add them later from Settings.")
            }

            Loader {
                id: openaiCard
                Layout.fillWidth: true
                sourceComponent: keyCard
                onLoaded: {
                    item.kind = "openai";
                    item.title = i18n.tr("OpenAI");
                    item.blurb = i18n.tr("Powers deep research, voice transcription, image generation and document OCR.");
                }
            }
            Loader {
                id: serperCard
                Layout.fillWidth: true
                sourceComponent: keyCard
                onLoaded: {
                    item.kind = "serper";
                    item.title = i18n.tr("Serper");
                    item.blurb = i18n.tr("Google web search results (serper.dev).");
                }
            }
            Loader {
                id: jinaCard
                Layout.fillWidth: true
                sourceComponent: keyCard
                onLoaded: {
                    item.kind = "jina";
                    item.title = i18n.tr("Jina");
                    item.blurb = i18n.tr("Web page reading (jina.ai).");
                }
            }

            RestartHint {
                id: restartHint
                Layout.fillWidth: true
                app: page.app
            }

            Button {
                Layout.fillWidth: true
                visible: page.wizardMode
                enabled: !page.committing
                color: theme.palette.normal.positive
                text: page.pendingCount > 0 ? i18n.tr("Verify, save & continue")
                                            : i18n.tr("Continue")
                onClicked: page.commitAndContinue()
            }
        }
    }
}
