import QtQuick 2.12
import QtQuick.Layouts 1.12
import Lomiri.Components 1.3

/*
 * Dashboard — health rows from setup-api status, start/stop/restart, the
 * last journal lines, CLI update, and the wizard entry point (when setup
 * is unfinished). Since the shell redesign (2026-08-29) this is an
 * always-alive view embedded in Main.qml's shell page: the shell header
 * owns the title, the Chat/Settings section tabs, and the Refresh action.
 */
Item {
    id: page
    property var app

    readonly property var api: app.api
    readonly property var service: api ? api.service : null
    readonly property bool serviceInstalled: service && service.supported === true
                                             && service.unit_installed === true

    property bool working: false
    property string resultText: ""
    property bool resultIsError: false
    property string journalText: ""

    function fail(text) { working = false; resultIsError = true; resultText = text; }

    function control(action) {
        working = true; resultIsError = false; resultText = "";
        var done = function(result, okText) {
            page.working = false;
            if (!result || result.ok !== true) {
                // describeError handles both shapes: setup-api's {code,
                // message} object and the bridge helpers' plain string.
                page.fail(page.app.describeError(result));
                return;
            }
            page.resultText = okText;
            page.app.refresh();
            page.loadJournal();
        };
        if (action === "restart")
            app.apiService({action: "restart"}, function(r) { done(r, i18n.tr("Briglia restarted.")); });
        else
            app.pyCall("systemctl_user", [action], function(r) {
                done(r, action === "start" ? i18n.tr("Briglia started.") : i18n.tr("Briglia stopped."));
            });
    }

    function loadJournal() {
        app.pyCall("tail_journal", [40], function(result) {
            if (result && result.ok === true)
                page.journalText = result.text || i18n.tr("(no journal entries yet)");
            else
                page.journalText = i18n.tr("Journal unavailable: %1")
                    .arg(result && result.error ? result.error : "?");
        });
    }

    Component.onCompleted: loadJournal()

    function row(label, value) { return { label: label, value: value }; }
    property var rows: {
        var list = [];
        if (app.detectInfo && app.detectInfo.installed)
            list.push(row(i18n.tr("Briglia CLI"), app.detectInfo.version));
        if (api) {
            list.push(row(i18n.tr("Setup"), api.setup.complete
                ? i18n.tr("complete") : i18n.tr("not finished yet")));
            if (api.providers && api.providers.active) {
                var profile = api.providers.profiles[api.providers.active];
                list.push(row(i18n.tr("Main model"),
                    (profile && profile.model ? profile.model : "?") + " · " + api.providers.active));
            }
            list.push(row(i18n.tr("Telegram"), api.telegram.configured
                ? i18n.tr("connected") : i18n.tr("not connected")));
            if (service && service.supported === true) {
                list.push(row(i18n.tr("Background service"), service.unit_installed
                    ? (service.active === "active" ? i18n.tr("running") : (service.active || i18n.tr("installed")))
                    : i18n.tr("not installed")));
                if (api.is_ubuntu_touch === true)
                    list.push(row(i18n.tr("Keep-awake"), service.wakelock_unit_installed === true
                        ? (service.wakelock_active === "active" ? i18n.tr("active") : i18n.tr("installed"))
                        : i18n.tr("not installed")));
            }
            if (api.toolchain && api.toolchain.tools) {
                var tcMissing = 0, tcPrefix = 0;
                for (var ti = 0; ti < api.toolchain.tools.length; ti++) {
                    var t = api.toolchain.tools[ti];
                    // optional components (pandoc, LibreOffice) only count
                    // when installed — absence is not "missing"
                    if (t.optional === true || t.package === "pandoc"
                            || t.package === "libreoffice") {
                        if (t.present === true && t.source === "prefix") tcPrefix++;
                        continue;
                    }
                    if (t.present !== true) tcMissing++;
                    else if (t.source === "prefix") tcPrefix++;
                }
                list.push(row(i18n.tr("Media toolchain"), tcMissing === 0
                    ? (tcPrefix > 0
                       ? i18n.tr("complete (%1 on userdata)").arg(tcPrefix)
                       : i18n.tr("complete"))
                    : i18n.tr("%1 tool(s) missing — install in Settings").arg(tcMissing)));
            }
            list.push(row(i18n.tr("Briglia process"), api.daemon_running === true
                ? i18n.tr("running") : i18n.tr("not running")));
            // Root-owned keep-awake unit of the previous identity, left in
            // place by an unprivileged migration (it still keeps the phone
            // awake): the swap is pending until the passcode step ran.
            if (app.legacyWakelockPresent)
                list.push(row(i18n.tr("Keep-awake (old unit)"),
                    i18n.tr("%1 still installed — swap pending").arg(app.legacy.wakelock_unit_name)));
        } else if (app.detectInfo && app.detectInfo.error) {
            list.push(row(i18n.tr("Problem"), app.detectInfo.error));
        }
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

            // ---- controls first (field feedback 2026-08-29 round 2: the
            // actions matter more than the status rows — no scrolling to
            // reach Start/Stop/Restart/Update)
            RowLayout {
                Layout.fillWidth: true
                visible: page.serviceInstalled
                Button {
                    Layout.fillWidth: true
                    enabled: !page.working && page.service && page.service.active !== "active"
                    color: theme.palette.normal.positive
                    text: i18n.tr("Start")
                    onClicked: page.control("start")
                }
                Button {
                    Layout.fillWidth: true
                    enabled: !page.working && page.service && page.service.active === "active"
                    text: i18n.tr("Stop")
                    onClicked: page.control("stop")
                }
                Button {
                    Layout.fillWidth: true
                    enabled: !page.working
                    text: i18n.tr("Restart")
                    onClicked: page.control("restart")
                }
            }

            Button {
                Layout.fillWidth: true
                visible: page.app.migrationNeeded
                color: theme.palette.normal.positive
                text: i18n.tr("Migrate Ada data to Briglia")
                onClicked: page.app.openMigrate()
            }

            Button {
                Layout.fillWidth: true
                visible: !page.app.migrationNeeded && page.app.legacyWakelockPresent
                text: i18n.tr("Finish keep-awake migration")
                onClicked: page.app.openMigrate("wakelock")
            }

            Button {
                Layout.fillWidth: true
                visible: page.api !== null && !page.app.migrationNeeded
                         && !(page.api.setup && page.api.setup.complete === true)
                color: theme.palette.normal.positive
                text: i18n.tr("Finish setup")
                onClicked: page.app.startWizard()
            }

            Button {
                Layout.fillWidth: true
                visible: !page.serviceInstalled && page.service && page.service.supported === true
                text: i18n.tr("Set up the background service")
                onClicked: page.app.pushPage("AlwaysOnPage.qml")
            }

            Button {
                Layout.fillWidth: true
                text: i18n.tr("Update Briglia CLI")
                onClicked: page.app.openInstall()
            }

            // ---- feedback for the buttons above
            Label {
                Layout.fillWidth: true
                visible: page.resultText !== ""
                wrapMode: Text.WordWrap
                color: page.resultIsError ? theme.palette.normal.negative
                                          : theme.palette.normal.positive
                text: page.resultText
            }

            // Outcome of the launch-time app auto-update (Settings toggle).
            Label {
                Layout.fillWidth: true
                visible: page.app.appUpdateNotice !== ""
                wrapMode: Text.WordWrap
                color: page.app.appUpdateNoticeError
                    ? theme.palette.normal.negative
                    : theme.palette.normal.positive
                text: page.app.appUpdateNotice
            }

            ActivityIndicator {
                Layout.alignment: Qt.AlignHCenter
                running: page.working
                visible: running
            }

            // ---- health rows
            Repeater {
                model: page.rows
                delegate: LomiriShape {
                    Layout.fillWidth: true
                    height: Math.max(units.gu(6), rowValue.height + units.gu(2))
                    aspect: LomiriShape.Flat
                    backgroundColor: theme.palette.normal.foreground
                    Label {
                        anchors { left: parent.left; leftMargin: units.gu(2); verticalCenter: parent.verticalCenter }
                        text: modelData.label
                        color: theme.palette.normal.backgroundSecondaryText
                    }
                    Label {
                        id: rowValue
                        anchors { right: parent.right; rightMargin: units.gu(2); verticalCenter: parent.verticalCenter }
                        width: parent.width * 0.55
                        horizontalAlignment: Text.AlignRight
                        wrapMode: Text.WordWrap
                        text: String(modelData.value)
                    }
                }
            }

            // Chat lives in its own section tab now; the only chat-related
            // note left here is the too-old-CLI hint.
            LomiriShape {
                Layout.fillWidth: true
                Layout.topMargin: units.gu(1)
                visible: !page.app.chatSupported && page.api !== null
                implicitHeight: chatHintLabel.height + units.gu(2)  // layout child: implicit, not height
                aspect: LomiriShape.Flat
                backgroundColor: theme.palette.normal.foreground
                Label {
                    id: chatHintLabel
                    anchors { left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter; margins: units.gu(1.5) }
                    wrapMode: Text.WordWrap
                    textSize: Label.Small
                    color: theme.palette.normal.backgroundSecondaryText
                    text: i18n.tr("💬 Chat needs Briglia CLI 0.2.0 or newer — tap “Update Briglia CLI” above, then come back.")
                }
            }

            // ---- journal
            Label {
                Layout.topMargin: units.gu(2)
                visible: page.serviceInstalled
                text: i18n.tr("Recent activity")
                textSize: Label.Large
            }
            LomiriShape {
                Layout.fillWidth: true
                visible: page.serviceInstalled
                implicitHeight: journalLabel.height + units.gu(2)  // layout child: implicit, not height
                aspect: LomiriShape.Flat
                backgroundColor: theme.palette.normal.foreground
                Label {
                    id: journalLabel
                    anchors { left: parent.left; right: parent.right; top: parent.top; margins: units.gu(1) }
                    wrapMode: Text.WrapAnywhere
                    textSize: Label.XSmall
                    font.family: "Ubuntu Mono"
                    text: page.journalText
                }
            }
            Button {
                Layout.fillWidth: true
                visible: page.serviceInstalled
                text: i18n.tr("Refresh log")
                onClicked: page.loadJournal()
            }
        }
    }
}
