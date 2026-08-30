import QtQuick 2.12
import QtQuick.Layouts 1.12
import Lomiri.Components 1.3

/*
 * Settings — the full read/edit surface. Every value setup ever wrote is
 * editable at any time; each section is a page shared with the wizard
 * (wizardMode: false), so there is exactly one editor per concern. The
 * web-search backend picker lives inline here. Since the shell redesign
 * (2026-08-29) this is an always-alive view embedded in Main.qml's shell
 * page; the shell header owns the title and section tabs.
 */
Item {
    id: page
    property var app

    readonly property var api: app.api

    property var backendIds: ["openrouter", "openai", "opencode"]
    property bool working: false
    property bool toolchainWorking: false
    property string toolchainResult: ""
    property bool toolchainError: false
    property string backendResult: ""
    property bool backendError: false
    property string bundleNote: ""

    // ---- app self-update state
    property bool updateWorking: false
    property string updateResult: ""
    property bool updateError: false
    property string availableUpdate: ""

    function entry(title, subtitle, file) {
        return { title: title, subtitle: subtitle, file: file };
    }
    property var entries: {
        var list = [];
        var providers = api && api.providers ? api.providers : null;
        list.push(entry(i18n.tr("Provider & model"),
            providers && providers.active
                ? providers.active + " · "
                  + (providers.profiles[providers.active] && providers.profiles[providers.active].model
                     ? providers.profiles[providers.active].model : "?")
                : i18n.tr("not configured"),
            "ProviderPage.qml"));
        var keysSet = [];
        if (api && api.keys) {
            if (api.keys.openai && api.keys.openai.set) keysSet.push("OpenAI");
            if (api.keys.serper && api.keys.serper.set) keysSet.push("Serper");
            if (api.keys.jina && api.keys.jina.set) keysSet.push("Jina");
        }
        list.push(entry(i18n.tr("API keys"),
            keysSet.length ? keysSet.join(" · ") : i18n.tr("none set"),
            "KeysPage.qml"));
        list.push(entry(i18n.tr("Name & email"),
            (api && api.identity && api.identity.user_name ? api.identity.user_name : i18n.tr("no name"))
            + " · " + (api && api.email_calendar ? api.email_calendar.provider : "none"),
            "IdentityPage.qml"));
        list.push(entry(i18n.tr("Telegram"),
            api && api.telegram && api.telegram.configured
                ? i18n.tr("connected") : i18n.tr("not connected"),
            "TelegramPage.qml"));
        list.push(entry(i18n.tr("Background service"),
            api && api.service && api.service.supported === true
                ? (api.service.unit_installed
                   ? (api.service.active === "active" ? i18n.tr("running") : i18n.tr("installed"))
                   : i18n.tr("not installed"))
                : i18n.tr("unsupported here"),
            "AlwaysOnPage.qml"));
        return list;
    }

    Flickable {
        anchors.fill: parent
        contentHeight: column.height + units.gu(6)
        clip: true

        ColumnLayout {
            id: column
            anchors { top: parent.top; topMargin: units.gu(2); horizontalCenter: parent.horizontalCenter }
            width: Math.min(parent.width - units.gu(4), units.gu(50))
            spacing: units.gu(1)

            Repeater {
                model: page.entries
                delegate: LomiriShape {
                    Layout.fillWidth: true
                    height: units.gu(8)
                    aspect: LomiriShape.Flat
                    backgroundColor: theme.palette.normal.foreground
                    ColumnLayout {
                        anchors { left: parent.left; leftMargin: units.gu(2); right: chevron.left; verticalCenter: parent.verticalCenter }
                        spacing: units.gu(0.2)
                        Label { text: modelData.title }
                        Label {
                            Layout.fillWidth: true
                            text: modelData.subtitle
                            textSize: Label.Small
                            elide: Text.ElideRight
                            color: theme.palette.normal.backgroundSecondaryText
                        }
                    }
                    Icon {
                        id: chevron
                        anchors { right: parent.right; rightMargin: units.gu(2); verticalCenter: parent.verticalCenter }
                        width: units.gu(2); height: units.gu(2)
                        name: "go-next"
                    }
                    MouseArea {
                        anchors.fill: parent
                        onClicked: page.app.pushPage(modelData.file, {wizardMode: false})
                    }
                }
            }

            // ---- key bundle scan
            Label {
                Layout.topMargin: units.gu(2)
                text: i18n.tr("Keys via QR")
                textSize: Label.Large
            }
            Label {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                textSize: Label.Small
                color: theme.palette.normal.backgroundSecondaryText
                text: i18n.tr("Generate QR codes from your keys at ada-app-psi.vercel.app/qr on a computer, then scan them here. Scanned keys pre-fill the sections above — each still verifies before saving.")
            }
            Button {
                Layout.fillWidth: true
                text: i18n.tr("Scan key bundle")
                onClicked: page.app.openScan("bundle", function(res) {
                    if (res && res.kind === "bundle")
                        page.bundleNote = page.app.acceptBundle(res);
                })
            }
            Button {
                Layout.fillWidth: true
                visible: page.app.scannedKeyCount > 0
                text: i18n.tr("Discard scanned keys (%1)").arg(page.app.scannedKeyCount)
                onClicked: {
                    page.app.clearScannedKeys();
                    page.bundleNote = i18n.tr("Scanned keys discarded.");
                }
            }
            Label {
                Layout.fillWidth: true
                visible: page.bundleNote !== ""
                wrapMode: Text.WordWrap
                textSize: Label.Small
                color: theme.palette.normal.positive
                text: page.bundleNote
            }

            // ---- web search backend (inline)
            Label {
                Layout.topMargin: units.gu(2)
                text: i18n.tr("Web search backend")
                textSize: Label.Large
            }
            Label {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                textSize: Label.Small
                color: theme.palette.normal.backgroundSecondaryText
                text: {
                    var ws = page.api && page.api.web_search ? page.api.web_search : null;
                    if (!ws) return i18n.tr("Which service answers Ada's web research.");
                    return i18n.tr("Which service answers Ada's web research. Current: %1%2")
                        .arg(ws.active)
                        .arg(ws.explicit === true ? "" : i18n.tr(" (automatic)"));
                }
            }
            OptionSelector {
                id: backendSelector
                Layout.fillWidth: true
                model: [i18n.tr("OpenRouter"), i18n.tr("OpenAI"), i18n.tr("OpenCode")]
                selectedIndex: {
                    var active = page.api && page.api.web_search ? page.api.web_search.active : "openai";
                    var i = page.backendIds.indexOf(active);
                    return i >= 0 ? i : 1;
                }
            }
            Label {
                Layout.fillWidth: true
                visible: page.backendResult !== ""
                wrapMode: Text.WordWrap
                textSize: Label.Small
                color: page.backendError ? theme.palette.normal.negative
                                         : theme.palette.normal.positive
                text: page.backendResult
            }
            Button {
                Layout.fillWidth: true
                enabled: !page.working
                text: i18n.tr("Save web search backend")
                onClicked: {
                    page.working = true; page.backendError = false; page.backendResult = "";
                    page.app.apiApply(
                        {web_search_backend: page.backendIds[backendSelector.selectedIndex]},
                        function(result) {
                            page.working = false;
                            if (!result || result.ok !== true) {
                                page.backendError = true;
                                page.backendResult = page.app.describeError(result);
                                return;
                            }
                            page.backendResult = i18n.tr("Saved.");
                            restartHint.lastResult = result;
                            page.app.refresh();
                        });
                }
            }

            // ---- media toolchain: PDF/image/audio-video tools, installed
            // to a USERDATA prefix (no sudo, nothing on the tiny read-only
            // rootfs, survives OS updates that wipe apt-installed packages)
            Label {
                Layout.topMargin: units.gu(2)
                text: i18n.tr("Media toolchain")
                textSize: Label.Large
            }
            Label {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                textSize: Label.Small
                color: theme.palette.normal.backgroundSecondaryText
                text: {
                    if (!page.api || !page.api.toolchain || !page.api.toolchain.tools)
                        return i18n.tr("State unknown — is Ada CLI installed?");
                    var missing = [];
                    var prefix = 0;
                    var pandocIn = false, loIn = false;
                    var tools = page.api.toolchain.tools;
                    for (var i = 0; i < tools.length; i++) {
                        var t = tools[i];
                        if (t.optional === true || t.package === "pandoc"
                                || t.package === "libreoffice") {
                            if (t.present === true) {
                                if (t.package === "pandoc") pandocIn = true;
                                if (t.package === "libreoffice") loIn = true;
                                if (t.source === "prefix") prefix++;
                            }
                            continue;
                        }
                        if (t.present !== true) missing.push(t.name);
                        else if (t.source === "prefix") prefix++;
                    }
                    var opt = " " + i18n.tr("Optional: pandoc %1, LibreOffice %2.")
                        .arg(pandocIn ? "✔" : "✖").arg(loIn ? "✔" : "✖");
                    if (missing.length === 0)
                        return (prefix > 0
                            ? i18n.tr("Complete — %1 tool(s) on the userdata partition (OTA-safe).").arg(prefix)
                            : i18n.tr("Complete — all tools present on the system.")) + opt;
                    return i18n.tr("Missing: %1. Installing places them on the userdata partition — no root, the OS partition is never touched, and they survive OS updates.")
                        .arg(missing.join(", ")) + opt;
                }
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: units.gu(1)
                visible: page.api && page.api.toolchain !== undefined
                CheckBox { id: pandocBox }
                Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    textSize: Label.Small
                    text: i18n.tr("Also install pandoc (document generation, ~150 MB)")
                }
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: units.gu(1)
                // the "optional" field marks a CLI (≥0.1.49) whose installer
                // knows the libreoffice flag — hide the box on older CLIs
                visible: page.api && page.api.toolchain !== undefined
                         && page.api.toolchain.tools !== undefined
                         && page.api.toolchain.tools.length > 0
                         && page.api.toolchain.tools[0].optional !== undefined
                CheckBox { id: libreofficeBox }
                Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    textSize: Label.Small
                    text: i18n.tr("Also install LibreOffice (DOCX/XLSX/PPTX → PDF, ~350 MB, headless)")
                }
            }
            Button {
                Layout.fillWidth: true
                visible: page.api && page.api.toolchain !== undefined
                enabled: !page.working && !page.toolchainWorking
                text: page.toolchainWorking
                      ? i18n.tr("Installing… (this can take several minutes)")
                      : i18n.tr("Install missing tools to userdata")
                onClicked: {
                    page.toolchainWorking = true;
                    page.toolchainError = false;
                    page.toolchainResult = i18n.tr("Downloading and installing — package lists and tools all go to userdata…");
                    page.app.apiApply({toolchain: {install: true, pandoc: pandocBox.checked,
                                                   libreoffice: libreofficeBox.checked}},
                        function(result) {
                            page.toolchainWorking = false;
                            if (!result || result.ok !== true) {
                                page.toolchainError = true;
                                page.toolchainResult = page.app.describeError(result);
                                return;
                            }
                            var notes = result.warnings && result.warnings.length > 0
                                        ? " " + result.warnings.join("; ") : "";
                            page.toolchainResult = i18n.tr("Done.") + notes;
                            page.app.refresh();
                        });
                }
            }
            Button {
                Layout.fillWidth: true
                // needs a CLI whose setup-api accepts {toolchain:{upgrade:true}}
                // (≥ 0.1.53) — advertised by the upgrade_supported marker
                visible: page.api && page.api.toolchain !== undefined
                         && page.api.toolchain.upgrade_supported === true
                enabled: !page.working && !page.toolchainWorking
                text: page.toolchainWorking
                      ? i18n.tr("Working… (this can take several minutes)")
                      : i18n.tr("Check for tool updates")
                onClicked: {
                    page.toolchainWorking = true;
                    page.toolchainError = false;
                    page.toolchainResult = i18n.tr("Checking the repositories for newer versions — rebuilds on userdata only if something is stale…");
                    page.app.apiApply({toolchain: {upgrade: true}},
                        function(result) {
                            page.toolchainWorking = false;
                            if (!result || result.ok !== true) {
                                page.toolchainError = true;
                                page.toolchainResult = page.app.describeError(result);
                                return;
                            }
                            var notes = result.warnings && result.warnings.length > 0
                                        ? result.warnings.join("; ") : i18n.tr("Done.");
                            page.toolchainResult = notes;
                            page.app.refresh();
                        });
                }
            }
            Label {
                Layout.fillWidth: true
                visible: page.toolchainResult !== ""
                wrapMode: Text.WordWrap
                textSize: Label.Small
                color: page.toolchainError ? theme.palette.normal.negative
                                           : theme.palette.normal.positive
                text: page.toolchainResult
            }

            // ---- app updates (the click itself; the Ada CLI updates from
            // the Dashboard's Update button)
            Label {
                Layout.topMargin: units.gu(2)
                text: i18n.tr("App updates")
                textSize: Label.Large
            }
            Label {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                textSize: Label.Small
                color: theme.palette.normal.backgroundSecondaryText
                text: i18n.tr("This app is v%1. Updates come from the Ada website; Ada itself updates from the Dashboard instead. After an app update, close and reopen the app.")
                    .arg(page.app.appVersion !== "" ? page.app.appVersion : "?")
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: units.gu(1)
                Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: i18n.tr("Update automatically when the app opens")
                }
                Switch {
                    id: autoUpdateSwitch
                    checked: page.app.appSettings
                        && page.app.appSettings.auto_update === true
                    onClicked: {
                        page.updateError = false;
                        page.updateResult = "";
                        var wanted = autoUpdateSwitch.checked;
                        page.app.setAppAutoUpdate(wanted, function(result) {
                            if (!result || result.ok !== true) {
                                // Revert the visual state; the setting did
                                // not persist.
                                autoUpdateSwitch.checked =
                                    page.app.appSettings
                                    && page.app.appSettings.auto_update === true;
                                page.updateError = true;
                                page.updateResult =
                                    page.app.describeError(result);
                            }
                        });
                    }
                }
            }
            Label {
                Layout.fillWidth: true
                visible: page.updateResult !== ""
                wrapMode: Text.WordWrap
                textSize: Label.Small
                color: page.updateError ? theme.palette.normal.negative
                                        : theme.palette.normal.positive
                text: page.updateResult
            }
            ActivityIndicator {
                Layout.alignment: Qt.AlignHCenter
                running: page.updateWorking
                visible: running
            }
            Button {
                Layout.fillWidth: true
                enabled: !page.updateWorking
                text: i18n.tr("Check for updates")
                onClicked: {
                    page.updateWorking = true;
                    page.updateError = false;
                    page.updateResult = "";
                    page.availableUpdate = "";
                    page.app.pyCall("app_update_check", [], function(result) {
                        page.updateWorking = false;
                        if (!result || result.ok !== true) {
                            page.updateError = true;
                            page.updateResult = page.app.describeError(result);
                            return;
                        }
                        if (result.update_available === true) {
                            page.availableUpdate = result.available;
                            page.updateResult = i18n.tr(
                                "Version %1 is available (you have v%2).")
                                .arg(result.available).arg(result.installed);
                        } else {
                            page.updateResult = i18n.tr(
                                "You already have the latest version (v%1).")
                                .arg(result.installed);
                        }
                    });
                }
            }
            Button {
                Layout.fillWidth: true
                visible: page.availableUpdate !== ""
                enabled: !page.updateWorking
                color: theme.palette.normal.positive
                text: i18n.tr("Install update v%1").arg(page.availableUpdate)
                onClicked: {
                    page.updateWorking = true;
                    page.updateError = false;
                    page.updateResult = "";
                    page.app.pyCall("app_update_install", [], function(result) {
                        page.updateWorking = false;
                        if (!result || result.ok !== true) {
                            page.updateError = true;
                            page.updateResult = page.app.describeError(result);
                            return;
                        }
                        page.availableUpdate = "";
                        if (result.updated === true) {
                            page.updateResult = i18n.tr(
                                "Updated to v%1 — close and reopen the app to start using it (Ada itself keeps running).")
                                .arg(result.available);
                        } else {
                            page.updateResult = i18n.tr(
                                "You already have the latest version (v%1).")
                                .arg(result.installed);
                        }
                    });
                }
            }

            // ---- guided setup rerun: for filling in anything skipped the
            // first time. Non-destructive by construction: wizard pages
            // prefill from stored state, masked keys stay unless replaced,
            // and blank fields keep the saved values.
            Label {
                Layout.topMargin: units.gu(2)
                text: i18n.tr("Guided setup")
                textSize: Label.Large
            }
            Label {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                textSize: Label.Small
                color: theme.palette.normal.backgroundSecondaryText
                text: i18n.tr("Walk through the setup steps again to fill in anything you skipped. Everything already configured stays as it is — leaving a field blank keeps the saved value.")
            }
            Button {
                Layout.fillWidth: true
                enabled: !page.working && !page.toolchainWorking
                text: i18n.tr("Re-run guided setup")
                onClicked: page.app.startWizard()
            }

            RestartHint {
                id: restartHint
                Layout.fillWidth: true
                app: page.app
            }
        }
    }
}
