import QtQuick 2.12
import Lomiri.Components 1.3
import Lomiri.Content 1.3

/*
 * "Open with…" — hands a file to another app through the Content Hub,
 * Ubuntu Touch's sanctioned way to open a document (there is no file://
 * handler in the URL dispatcher). The picker lists every installed app
 * registered as a destination for the file's content category; choosing
 * one charges a transfer with the file and the OS performs the handover.
 */
Page {
    id: page
    property var app
    property string path: ""

    header: PageHeader {
        id: pageHeader
        title: i18n.tr("Open with…")
        subtitle: String(page.path).split("/").pop()
    }

    // Extension → hub category, so the right handlers show up (Documents
    // lists Document Viewer for a PDF, Pictures lists the Gallery, …).
    // Unknown extensions fall back to Documents — the broadest set of
    // viewer apps registers there.
    function hubTypeFor(p) {
        var ext = String(p).split(".").pop().toLowerCase();
        if (["jpg", "jpeg", "png", "gif", "webp", "bmp", "heic", "svg"].indexOf(ext) !== -1)
            return ContentType.Pictures;
        if (["mp3", "ogg", "opus", "wav", "m4a", "flac", "aac"].indexOf(ext) !== -1)
            return ContentType.Music;
        if (["mp4", "mov", "mkv", "webm", "avi", "m4v"].indexOf(ext) !== -1)
            return ContentType.Videos;
        return ContentType.Documents;
    }

    Component {
        id: exportItem
        ContentItem {}
    }

    ContentPeerPicker {
        anchors { top: pageHeader.bottom; left: parent.left; right: parent.right; bottom: parent.bottom }
        visible: true
        showTitle: false
        contentType: page.hubTypeFor(page.path)
        handler: ContentHandler.Destination
        onPeerSelected: {
            var transfer = peer.request();
            transfer.items = [exportItem.createObject(page, {"url": "file://" + page.path})];
            transfer.state = ContentTransfer.Charged;
            page.app.popPage();
        }
        onCancelPressed: page.app.popPage()
    }
}
