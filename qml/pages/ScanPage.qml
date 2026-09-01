import QtQuick 2.12
import QtQuick.Layouts 1.12
import QtMultimedia 5.12
import Lomiri.Components 1.3

/*
 * QR scanner (docs/QR_KEYS_SPEC.md). Two modes:
 *  - "single": returns the first decoded QR text (any code) — used by the
 *    per-field Scan buttons.
 *  - "bundle": assembles ADAK1 key-bundle frames from the /qr generator
 *    page (auto-cycling codes on the computer screen) and returns the
 *    parsed keys.
 *
 * Decoding is the pure-Python py/qr_scan.py: the viewfinder is grabbed to
 * a PNG (grabToImage is the one capture path that yields a stdlib-readable
 * format) roughly once a second and handed to the bridge. The frame file
 * is deleted by the decoder after each read.
 *
 * Photo-mode fallback: on Ubuntu Touch phone hardware the camera preview
 * is often an external GPU texture that grabToImage renders as a uniform
 * blank block — the decoder reports those frames as `blank`, and after a
 * few in a row the page switches to real still captures
 * (imageCapture.captureToLocation → JPEG → hidden Image → grabToImage →
 * PNG → decoder). Slower per frame, but it reads the actual sensor.
 */
Page {
    id: page
    property var app
    property string mode: "single"
    // The result callback lives in app.scanCallback — see openScan() in
    // Main.qml for why it must not be passed through push properties.

    property bool busy: false
    property bool finished: false
    property string statusText: mode === "bundle"
        ? i18n.tr("Point the camera at the key codes on your computer screen.")
        : i18n.tr("Point the camera at the QR code.")
    property bool statusIsError: false
    property int have: 0
    property int total: 0

    header: PageHeader {
        title: page.mode === "bundle" ? i18n.tr("Scan key bundle")
                                      : i18n.tr("Scan QR code")
    }

    Component.onCompleted: {
        if (mode === "bundle")
            app.pyCall("qr_scan.reset_session", [], null);
        debugEnabled = app.scanDebug === true;
        if (debugEnabled)
            logHeader();
    }

    function logHeader() {
        app.pyCall("qr_scan.env_info", [], function(env) {
            page.debugLog("=== diagnostics on · app v"
                + (page.app.appVersion !== "" ? page.app.appVersion : "?")
                + " · " + env + " · mode "
                + (page.photoMode ? "photo" : "live")
                + " · camera status " + camera.cameraStatus
                + " · error '" + (camera.errorString || "") + "'");
        });
    }

    // Callback runs BEFORE the page pops: the invoking page sits beneath
    // this one and is alive either way, but running it after popPage()
    // leaves zero evidence if anything in the chain goes wrong — and on
    // real hardware (field round 3, 2026-08-28) a scan visibly closed
    // the page while the key never reached the field. Both the delivery
    // and the callback outcome are logged.
    function deliver(result) {
        if (finished) return;
        finished = true;
        camera.stop();
        debugLog("delivering kind=" + result.kind
                 + (result.kind === "text"
                    ? " (" + ("" + (result.text || "")).length + " chars)" : ""));
        var cb = app.scanCallback;
        app.scanCallback = null;
        if (typeof cb === "function") {
            var cbError = "";
            try { cb(result); } catch (e) { cbError = "" + e; }
            debugLog(cbError ? "DELIVERY CALLBACK FAILED: " + cbError
                             : "delivery callback ran successfully");
        } else {
            // must never happen again — this exact silence cost a round
            debugLog("NO delivery callback registered (" + typeof cb
                     + ") — result dropped");
        }
        app.popPage();
    }

    // Viewfinder grabs that come back with zero contrast mean this device's
    // camera preview cannot be captured from the scene graph — switch to
    // taking real photos instead (see header comment).
    property bool photoMode: false
    property int blankStreak: 0
    // grabToImage FAILING outright (null result / failed save / refused
    // start) is just as much a "this preview can't be read" symptom as
    // blank frames — devices exist where the external-texture grab errors
    // instead of returning a uniform block, and they must reach photo
    // mode too (field finding, 2026-08-28).
    property int grabFailStreak: 0

    // Diagnostics counters (surfaced by the debug row; cheap to keep
    // always so a field report can quote them even before debug is on).
    property int dFrames: 0
    property int dGrabFails: 0
    property int dBlanks: 0
    property int dNoFinds: 0
    property int dErrors: 0
    property int dPhotos: 0
    property int dLastMs: 0
    property int dLastCands: 0
    property string dLastDim: ""
    property bool debugEnabled: false

    function debugLog(msg) {
        if (debugEnabled)
            app.pyCall("qr_scan.log_event", [msg, true], null);
    }

    function noteGrabFail(why) {
        dGrabFails++;
        debugLog("grab failed: " + why);
        if (!photoMode) {
            grabFailStreak++;
            if (grabFailStreak >= 2) {
                enterPhotoMode();
                return;
            }
        }
        statusIsError = true;
        statusText = i18n.tr("Could not capture the camera view — retrying…");
    }

    function enterPhotoMode() {
        photoMode = true;
        statusIsError = false;
        debugLog("switching to photo mode (blanks " + blankStreak
                 + ", grab fails " + grabFailStreak + ")");
        statusText = i18n.tr("This device's live preview can't be read directly — switched to photo mode: a picture is taken every couple of seconds. Hold the phone steady on the code.");
    }

    function handle(res) {
        if (finished) return;
        if (!res) {
            statusIsError = true;
            statusText = i18n.tr("The scanner backend did not answer.");
            return;
        }
        dFrames++;
        if (res.ms !== undefined) dLastMs = res.ms;
        if (res.cands !== undefined) dLastCands = res.cands;
        if (res.dim !== undefined) dLastDim = res.dim;
        if (res.found !== true) {
            if (res.blank === true) {
                dBlanks++;
                if (!photoMode) {
                    blankStreak++;
                    if (blankStreak >= 3)
                        enterPhotoMode();
                }
                return;
            }
            blankStreak = 0;
            if (res.error) {
                dErrors++;
                statusIsError = true;
                statusText = i18n.tr("Frame problem: %1").arg(res.error);
                return;
            }
            dNoFinds++;
            return;  // keep scanning quietly
        }
        if (res.kind === "text") {
            deliver(res);
            return;
        }
        if (res.kind === "bundle" && res.done === true) {
            deliver(res);
            return;
        }
        if (res.kind === "bundle") {
            have = res.have; total = res.total;
            statusIsError = false;
            statusText = i18n.tr("Captured %1 of %2 codes — keep the camera on the screen while they cycle.")
                         .arg(res.have).arg(res.total);
            return;
        }
        // not_bundle / bundle_mismatch / bundle_corrupt / bundle_in_single
        statusIsError = true;
        statusText = res.message || i18n.tr("Unexpected code.");
    }

    // Stale-busy watchdog: grabToImage can also LOSE its callback (device
    // codec quirks), which would freeze scanning with busy stuck true —
    // any capture older than this is abandoned and scanning resumes.
    // Abandoning bumps captureGen, and every async continuation of a
    // capture re-checks its generation before touching ANY state: a late
    // callback from an abandoned capture must be a pure no-op, or it
    // races the newer capture (Codex, 2026-08-28). Frame paths embed the
    // generation too, so a stale saveToFile cannot clobber the file a
    // newer capture is decoding.
    property double busyStart: 0
    property int captureGen: 0
    // true while Python is decoding a captured frame — the slow-but-
    // healthy phase on weak CPUs; see the two-phase watchdog in grab().
    property bool decoding: false

    function grab() {
        if (finished || !page.visible) return;
        if (busy) {
            // Two-phase watchdog (field finding 2026-08-28, round 2): the
            // CAPTURE phase hanging is a device symptom worth demoting to
            // photo mode, but the DECODE phase merely being slow is not —
            // a Pixel 3a took 7.8s to decode a frame that then SUCCEEDED,
            // and the old single 5s watchdog labeled that working decode
            // "grab failed" and would have demoted a working live path.
            // Decodes get a huge allowance and never count as failures.
            var limit = decoding ? 60000 : (photoMode ? 10000 : 5000);
            if (Date.now() - busyStart > limit) {
                captureGen++;  // orphan the stuck capture's callbacks
                busy = false;
                if (decoding) {
                    decoding = false;
                    debugLog("decode timed out (60s) — abandoned");
                } else if (photoMode) {
                    debugLog("photo pipeline timed out (" + limit
                             + "ms) — retrying");
                } else {
                    noteGrabFail("capture timed out — grabToImage callback "
                                 + "never fired");
                }
            }
            return;
        }
        if (photoMode) {
            photoGrab();
            return;
        }
        busy = true;
        busyStart = Date.now();
        captureGen++;
        var gen = captureGen;
        app.pyCall("qr_scan.frame_path", [gen], function(path) {
            if (gen !== page.captureGen || page.finished) return;
            if (!path) { page.busy = false; return; }
            var w = Math.min(Math.max(320, viewfinder.width), 720);
            var h = Math.round(w * viewfinder.height / Math.max(1, viewfinder.width));
            var started = viewfinder.grabToImage(function(result) {
                if (gen !== page.captureGen || page.finished) return;
                if (!result || !result.saveToFile(path)) {
                    page.busy = false;
                    page.noteGrabFail(!result ? "null result" : "saveToFile failed");
                    return;
                }
                page.grabFailStreak = 0;
                page.decoding = true;
                page.busyStart = Date.now();
                page.app.pyCall("qr_scan.scan_png",
                                [path, page.mode, page.debugEnabled],
                                function(res) {
                    // A decode that outlived its generation (the 60s
                    // escape hatch fired while Python was still working)
                    // is still a valid decode — deliver it rather than
                    // discarding a successful scan.
                    if (page.finished) return;
                    if (gen !== page.captureGen) {
                        if (res && res.found === true)
                            page.handle(res);
                        return;
                    }
                    page.decoding = false;
                    page.busy = false;
                    page.handle(res);
                });
            }, Qt.size(w, h));
            // grabToImage returns false when the capture cannot even
            // start — no callback will ever fire, so release busy HERE
            // or scanning freezes permanently (Codex, 2026-08-28).
            if (started === false && gen === page.captureGen) {
                page.busy = false;
                page.noteGrabFail("grabToImage refused to start");
            }
        });
    }

    // ---- photo-mode pipeline: real still capture → JPEG → hidden Image
    // (a normal scene-graph texture, so grabToImage works on it) → PNG →
    // decoder. Every continuation re-checks its capture generation.
    property string pendingPhotoJpg: ""

    function photoGrab() {
        busy = true;
        busyStart = Date.now();
        captureGen++;
        var gen = captureGen;
        app.pyCall("qr_scan.photo_path", [gen], function(jpg) {
            if (gen !== page.captureGen || page.finished) return;
            if (!jpg) { page.busy = false; return; }
            page.pendingPhotoJpg = jpg;
            camera.imageCapture.captureToLocation(jpg);
        });
    }

    function photoSaved(path) {
        if (finished || path !== pendingPhotoJpg) return;
        dPhotos++;
        debugLog("photo saved: " + path);
        photoImage.source = "";   // force a reload even on an equal URL
        photoImage.source = "file://" + path;
    }

    function photoLoaded() {
        var gen = captureGen;
        var jpg = pendingPhotoJpg;
        app.pyCall("qr_scan.frame_path", [gen], function(png) {
            if (gen !== page.captureGen || page.finished) return;
            if (!png) { page.busy = false; return; }
            var started = photoImage.grabToImage(function(result) {
                if (gen !== page.captureGen || page.finished) return;
                page.app.pyCall("qr_scan.finish_photo",
                                [jpg, page.debugEnabled], null);
                if (!result || !result.saveToFile(png)) {
                    page.busy = false;
                    page.dGrabFails++;
                    page.debugLog("photo re-grab failed: "
                                  + (!result ? "null result" : "saveToFile failed"));
                    return;
                }
                page.decoding = true;
                page.busyStart = Date.now();
                page.app.pyCall("qr_scan.scan_png",
                                [png, page.mode, page.debugEnabled],
                                function(res) {
                    if (page.finished) return;
                    if (gen !== page.captureGen) {
                        if (res && res.found === true)
                            page.handle(res);
                        return;
                    }
                    page.decoding = false;
                    page.busy = false;
                    page.handle(res);
                });
            }, Qt.size(960, Math.round(960 * photoImage.height
                                       / Math.max(1, photoImage.width))));
            if (started === false && gen === page.captureGen) {
                page.app.pyCall("qr_scan.finish_photo",
                                [jpg, page.debugEnabled], null);
                page.busy = false;
                page.debugLog("photo re-grab refused to start");
            }
        });
    }

    Camera {
        id: camera
        captureMode: Camera.CaptureStillImage
        cameraState: Camera.ActiveState
        focus {
            focusMode: CameraFocus.FocusContinuous
            focusPointMode: CameraFocus.FocusPointAuto
        }
        imageCapture {
            onImageSaved: page.photoSaved(path)
            onCaptureFailed: {
                if (page.finished) return;
                page.busy = false;
                page.dErrors++;
                page.debugLog("photo capture FAILED: " + message);
                page.statusIsError = true;
                page.statusText = i18n.tr("Photo capture failed: %1").arg(message);
            }
        }
    }

    // Behind the viewfinder: only visible when the preview itself renders
    // nothing, in which case the last captured photo doubles as an aiming
    // aid. Must stay visible:true — grabToImage needs it in the scene.
    Image {
        id: photoImage
        anchors.fill: viewfinder
        z: -1
        fillMode: Image.PreserveAspectFit
        sourceSize.width: 1280
        cache: false
        asynchronous: true
        onStatusChanged: {
            if (page.finished || !page.photoMode) return;
            if (status === Image.Ready)
                page.photoLoaded();
            else if (status === Image.Error)
                page.busy = false;
        }
    }

    VideoOutput {
        id: viewfinder
        anchors { top: page.header.bottom; left: parent.left; right: parent.right; bottom: panel.top }
        source: camera
        fillMode: VideoOutput.PreserveAspectCrop
        autoOrientation: true
    }

    Timer {
        interval: 800
        repeat: true
        running: page.visible && !page.finished
        onTriggered: page.grab()
    }

    Rectangle {
        id: panel
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom }
        height: panelColumn.height + units.gu(3)
        color: theme.palette.normal.background

        ColumnLayout {
            id: panelColumn
            anchors { top: parent.top; topMargin: units.gu(1.5); horizontalCenter: parent.horizontalCenter }
            width: Math.min(parent.width - units.gu(4), units.gu(50))
            spacing: units.gu(1)

            Label {
                Layout.fillWidth: true
                visible: camera.errorString !== undefined && camera.errorString !== ""
                wrapMode: Text.WordWrap
                textSize: Label.Small
                color: theme.palette.normal.negative
                text: camera.errorString ? i18n.tr("Camera problem: %1").arg(camera.errorString) : ""
            }

            Label {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                textSize: Label.Small
                color: page.statusIsError ? theme.palette.normal.negative
                                          : theme.palette.normal.backgroundSecondaryText
                text: page.statusText
            }

            ProgressBar {
                Layout.fillWidth: true
                visible: page.mode === "bundle" && page.total > 1
                minimumValue: 0
                maximumValue: Math.max(1, page.total)
                value: page.have
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: units.gu(1)
                Switch {
                    id: debugSwitch
                    checked: page.debugEnabled
                    onCheckedChanged: {
                        var turningOn = checked && !page.debugEnabled;
                        page.debugEnabled = checked;
                        page.app.scanDebug = checked;
                        if (turningOn)
                            page.logHeader();
                    }
                }
                Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    textSize: Label.Small
                    text: i18n.tr("Diagnostics (saves camera frames to Documents/briglia-qr-debug)")
                }
            }

            Label {
                Layout.fillWidth: true
                visible: page.debugEnabled
                wrapMode: Text.WordWrap
                textSize: Label.XSmall
                color: theme.palette.normal.backgroundSecondaryText
                text: (page.photoMode ? "photo" : "live")
                      + " · frames " + page.dFrames
                      + " · grab fails " + page.dGrabFails
                      + " · blank " + page.dBlanks
                      + " · no QR " + page.dNoFinds
                      + " · errors " + page.dErrors
                      + " · photos " + page.dPhotos
                      + (page.dLastDim
                         ? " · last " + page.dLastDim + " " + page.dLastMs
                           + "ms, " + page.dLastCands + " finder candidates"
                         : "")
                      + " · cam " + camera.cameraStatus
            }

            Button {
                Layout.fillWidth: true
                text: i18n.tr("Cancel")
                onClicked: {
                    page.finished = true;
                    camera.stop();
                    page.app.scanCallback = null;
                    page.app.popPage();
                }
            }
        }
    }
}
