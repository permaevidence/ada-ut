import QtQuick 2.12
import QtQuick.Layouts 1.12
import Lomiri.Components 1.3

/*
 * Shown after a successful apply while the daemon was running
 * (setup-api's restart_needed): changes reach `ada daemon` only after a
 * restart. The button uses `setup-api service restart`, which exists only
 * where the service does (Linux) — elsewhere the page just shows the hint.
 */
ColumnLayout {
    id: hint
    property var app
    // The last apply response; visibility derives from it.
    property var lastResult: null
    property string message: ""
    property bool restarting: false

    // Stays visible for the outcome message after the pending state clears.
    visible: message !== "" || (lastResult !== null && lastResult.ok === true
             && lastResult.restart_needed === true)
    spacing: units.gu(1)

    Label {
        Layout.fillWidth: true
        wrapMode: Text.WordWrap
        color: theme.palette.normal.backgroundSecondaryText
        text: hint.message !== ""
              ? hint.message
              : i18n.tr("Ada is running — restart it so the change takes effect.")
    }

    Button {
        Layout.fillWidth: true
        visible: hint.lastResult !== null
                 && hint.app && hint.app.api && hint.app.api.service
                 && hint.app.api.service.supported === true
                 && hint.app.api.service.unit_installed === true
        enabled: !hint.restarting
        text: hint.restarting ? i18n.tr("Restarting…") : i18n.tr("Restart Ada now")
        onClicked: {
            hint.restarting = true;
            hint.app.apiService({action: "restart"}, function(result) {
                hint.restarting = false;
                if (result && result.ok) {
                    hint.message = i18n.tr("Ada restarted — the change is live.");
                    hint.lastResult = null;
                    hint.app.refresh();
                } else {
                    hint.message = hint.app.describeError(result);
                }
            });
        }
    }
}
