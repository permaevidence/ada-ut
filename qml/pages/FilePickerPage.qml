import QtQuick 2.12
import QtQuick.Layouts 1.12
import Lomiri.Components 1.3

/*
 * Minimal attachment picker for the chat page. The app is unconfined, so a
 * plain directory browser over the home folders does the job without
 * ContentHub choreography: shortcuts to the places phone files actually
 * live, tap a folder to enter it, tap a file to hand its path back.
 */
Page {
    id: page
    property var app
    // callback(path) — invoked with the picked file's absolute path
    property var callback

    property string currentPath: ""
    property string parentPath: ""
    property var entries: []
    property string loadError: ""

    header: PageHeader {
        id: pageHeader
        title: i18n.tr("Pick a file")
        subtitle: page.currentPath
    }

    function load(path) {
        app.pyCall("list_dir", [path], function(result) {
            if (!result || result.ok !== true) {
                page.loadError = result && result.error
                    ? result.error : i18n.tr("could not open folder");
                return;
            }
            page.loadError = "";
            page.currentPath = result.path;
            page.parentPath = result.parent || "/";
            page.entries = result.entries || [];
        });
    }

    function formatSize(bytes) {
        if (bytes < 1024) return bytes + " B";
        if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " KB";
        return (bytes / (1024 * 1024)).toFixed(1) + " MB";
    }

    Component.onCompleted: load("~")

    // shortcut row
    Flow {
        id: shortcuts
        anchors { top: pageHeader.bottom; left: parent.left; right: parent.right; margins: units.gu(1) }
        spacing: units.gu(0.5)
        Repeater {
            model: [
                { label: i18n.tr("Home"), path: "~" },
                { label: i18n.tr("Pictures"), path: "~/Pictures" },
                { label: i18n.tr("Downloads"), path: "~/Downloads" },
                { label: i18n.tr("Documents"), path: "~/Documents" }
            ]
            delegate: Button {
                text: modelData.label
                onClicked: page.load(modelData.path)
            }
        }
    }

    Label {
        id: errorLabel
        anchors { top: shortcuts.bottom; left: parent.left; right: parent.right; margins: units.gu(1) }
        visible: page.loadError !== ""
        wrapMode: Text.WordWrap
        color: theme.palette.normal.negative
        text: page.loadError
    }

    ListView {
        anchors {
            top: errorLabel.visible ? errorLabel.bottom : shortcuts.bottom
            left: parent.left
            right: parent.right
            bottom: parent.bottom
            margins: units.gu(1)
        }
        clip: true
        // ".." row + entries
        model: 1 + page.entries.length

        delegate: ListItem {
            readonly property bool isUp: index === 0
            readonly property var entry: isUp ? null : page.entries[index - 1]
            height: units.gu(6)
            onClicked: {
                if (isUp) {
                    page.load(page.parentPath);
                } else if (entry.dir) {
                    page.load(entry.path);
                } else {
                    var cb = page.callback;
                    page.app.popPage();
                    if (cb) cb(entry.path);
                }
            }
            RowLayout {
                anchors { fill: parent; leftMargin: units.gu(2); rightMargin: units.gu(2) }
                spacing: units.gu(1)
                Label {
                    text: isUp ? "⬆" : (entry.dir ? "📁" : "📄")
                }
                Label {
                    Layout.fillWidth: true
                    elide: Text.ElideMiddle
                    text: isUp ? i18n.tr("Up one folder") : entry.name
                }
                Label {
                    visible: !isUp && !entry.dir
                    textSize: Label.Small
                    color: theme.palette.normal.backgroundSecondaryText
                    text: isUp || entry.dir ? "" : page.formatSize(entry.size)
                }
            }
        }
    }
}
