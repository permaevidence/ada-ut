import QtQuick 2.12
import Lomiri.Components 1.3

/*
 * Full-screen photo view for a chat image. Tap the photo to go back; the
 * header action hands the file to another app (the Gallery has zoom,
 * sharing, …) through the Open-with dialog.
 */
Page {
    id: page
    property var app
    property string path: ""

    header: PageHeader {
        id: pageHeader
        title: String(page.path).split("/").pop()
        trailingActionBar.actions: [
            Action {
                iconName: "share"
                text: i18n.tr("Open with…")
                onTriggered: page.app.pushPage("OpenWithPage.qml", {path: page.path})
            }
        ]
    }

    Rectangle {
        anchors { top: pageHeader.bottom; left: parent.left; right: parent.right; bottom: parent.bottom }
        color: "black"

        Image {
            anchors.fill: parent
            fillMode: Image.PreserveAspectFit
            asynchronous: true
            source: page.path !== "" ? "file://" + page.path : ""
        }

        MouseArea {
            anchors.fill: parent
            onClicked: page.app.popPage()
        }
    }
}
