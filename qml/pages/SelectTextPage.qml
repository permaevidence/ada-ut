import QtQuick 2.12
import Lomiri.Components 1.3

/*
 * Full-screen selectable view of one message. Bubbles render with Label,
 * which has no selection support — and a selectable text item inside
 * every bubble would fight the list's flicking for touch grabs. So
 * press-and-hold on a bubble lands here instead: a read-only TextArea
 * with the normal Lomiri selection handles (press-and-hold a word to
 * start selecting), plus a copy-the-whole-message header action.
 */
Page {
    id: page
    property var app
    property string text: ""

    property string copied: ""
    Timer {
        id: copiedTimer
        interval: 2500
        onTriggered: page.copied = ""
    }

    header: PageHeader {
        id: pageHeader
        title: i18n.tr("Select text")
        subtitle: page.copied !== ""
                  ? page.copied
                  : i18n.tr("Press and hold a word, then drag the handles")
        trailingActionBar.actions: [
            Action {
                iconName: "edit-select-all"
                text: i18n.tr("Select all")
                onTriggered: selArea.selectAll()
            },
            Action {
                iconName: "edit-copy"
                text: i18n.tr("Copy all")
                onTriggered: {
                    Clipboard.push(page.text);
                    page.copied = i18n.tr("Copied the whole message");
                    copiedTimer.restart();
                }
            }
        ]
    }

    TextArea {
        id: selArea
        anchors { top: pageHeader.bottom; left: parent.left; right: parent.right; bottom: parent.bottom; margins: units.gu(1) }
        autoSize: false
        readOnly: true
        wrapMode: TextEdit.Wrap
        text: page.text

        // Field finding (Pixel): long-press selected a word but the
        // selection could never be EXTENDED. Lomiri's draggable selection
        // handles (TextCursor.qml) are gated on cursorVisible, and Qt's
        // TextEdit forces cursorVisible to false for read-only fields —
        // so the handles simply never appeared. Qt writes the property
        // imperatively (a declarative binding would not be reasserted),
        // so pin it back to true after every internal write. The
        // reassert-inside-its-own-change-handler terminates: setting
        // true when it flipped false is the only write it ever makes.
        function keepHandlesVisible() {
            if (!cursorVisible) cursorVisible = true;
        }
        Component.onCompleted: keepHandlesVisible()
        onActiveFocusChanged: keepHandlesVisible()
        onSelectedTextChanged: keepHandlesVisible()
        onCursorVisibleChanged: keepHandlesVisible()
    }
}
