import QtQuick 2.12
import QtQuick.Layouts 1.12
import Lomiri.Components 1.3
import "../ChatDraftLogic.js" as DraftLogic
import "../MarkdownLogic.js" as Markdown

/*
 * Chat with Briglia — a live window onto the Briglia CLI conversation over the
 * companion-app socket (briglia-cli docs/UT_CHAT_PLAN.md).
 *
 * The page is a dumb renderer of server events: history arrives in the
 * `hello` snapshot, live turns as `message` upserts (queued mid-turn
 * messages get re-sent with queued=false once dispatched), agent state as
 * `status`. Input goes back as `send` / `voice` / `stop` / `command`
 * requests. The only local rows are the voice "transcribing…" placeholder
 * (replaced by the real transcribed message event) and /command echo+result,
 * so a killed app loses nothing: reopening replays everything from history.
 *
 * Since the shell redesign (2026-08-29) this is an always-alive view
 * embedded in Main.qml's shell page, not a pushed Page: tab switches only
 * toggle visibility, so the socket, scroll position and composer survive
 * them. The socket connects on activate() — called when the shell first
 * becomes home — never at declaration, so a not-yet-set-up phone doesn't
 * spin a doomed reconnect loop during the wizard.
 */
Item {
    id: page
    property var app

    // ---- connection + agent state (from events)
    property string connState: "connecting"
    property bool turnActive: false
    property string activityText: ""
    property bool privacyOn: false
    property string imagesDir: ""
    property string documentsDir: ""
    property bool sawHello: false

    // ---- transient banner (errors / notices)
    property string banner: ""
    property bool bannerIsError: false

    // ---- composer + attachments + recording
    property var pendingAttachments: []
    property bool recording: false
    property int recordSeconds: 0
    property int recordMax: 300
    // tag -> recorded file path, deleted after Briglia's ack/nack. Registered
    // BEFORE send_voice is invoked, so an instant response can't miss it.
    property var voiceFiles: ({})
    // tag -> {text, attachments, sent, cleared, outcome, error} for sends awaiting
    // Briglia's durable ack. The tag is OURS, generated and registered here
    // synchronously before Python ever touches the socket (Python maps the
    // wire ref to it pre-transmission), so no ack/nack can arrive before its
    // request is known — the race Codex found in round 2. `sent` records
    // that the pyCall callback confirmed the wire write and cleared the
    // composer; an ack/nack landing first parks its outcome in `outcome`
    // for the callback to consume. `cleared` records whether the composer
    // text was ACTUALLY cleared for the entry (round 4: clearing is
    // conditional on the composer still matching the snapshot). Content is
    // RESTORED to the composer on refusal or connection loss — an ack means
    // Briglia durably persisted the message, so nothing typed can vanish.
    property var pendingSends: ({})
    property int localSerial: 0
    // True from Send tap until its callback settles (round 4, Codex): the
    // Send button is disabled meanwhile, so a rapid double-tap can't submit
    // the same message twice. The guard timer is a safety release in case a
    // bridge callback is ever lost — a stuck flag must not brick the chat.
    property bool sendInFlight: false
    Timer {
        id: sendGuardTimer
        interval: 20000
        onTriggered: page.sendInFlight = false
    }

    // Plain JS object captured by every async pyCall callback in this page.
    // A result can land AFTER the page was popped, and a destroyed page's
    // QML context throws on any unqualified lookup (`i18n`, ids) — field-
    // tested: the final draft-save callback fired after back-navigation and
    // its i18n lookup surfaced as a fake red "startup problem" on the
    // Welcome page. Closure-captured JS locals survive destruction, so each
    // callback captures this token at call time and no-ops once
    // Component.onDestruction flips `alive`. The underlying Python calls
    // still run to completion — only the UI reaction is skipped.
    property var lifeToken: ({ alive: true })

    ListModel { id: chatModel }
    // message id -> model row (upserts); plain JS object, never bound
    property var idIndex: ({})

    // Pixels between the viewport bottom and the content's true bottom
    // edge. ListView REBASES its content coordinates as variable-height
    // delegates get measured (originY drifts away from 0), so the naive
    // contentHeight - contentY - height is computed against the wrong
    // basis — the field bug where the jump-to-latest arrow never showed.
    // Content spans [originY, originY + contentHeight]; the viewport is
    // [contentY, contentY + height].
    readonly property real distanceFromLatest:
        (listView.originY + listView.contentHeight)
        - (listView.contentY + listView.height)

    // ------------------------------------------------------------- events

    function showBanner(text, isError) {
        banner = text || "";
        bannerIsError = isError === true;
        bannerTimer.restart();
    }
    Timer {
        id: bannerTimer
        interval: 12000
        onTriggered: page.banner = ""
    }

    function handleEvent(ev) {
        if (!ev || !ev.type) return;
        switch (ev.type) {
        case "_connection":
            connState = ev.state || "disconnected";
            if (connState !== "connected")
                failUnconfirmedRequests();
            break;
        case "hello":
            sawHello = true;
            imagesDir = ev.images_dir || "";
            documentsDir = ev.documents_dir || "";
            privacyOn = ev.privacy === true;
            turnActive = ev.turn_active === true;
            chatModel.clear();
            idIndex = ({});
            var history = ev.history || [];
            for (var i = 0; i < history.length; i++)
                upsertMessage(history[i]);
            scrollToEnd();
            break;
        case "message": {
            // Auto-follow only while the user is at (or near) the latest
            // message — captured BEFORE the upsert grows contentHeight.
            // Reading older history must not get yanked to the bottom on
            // every live event; the floating arrow is the way back down.
            var nearEnd = page.distanceFromLatest < units.gu(10);
            upsertMessage(ev);
            if (nearEnd) scrollToEnd();
            break;
        }
        case "status":
            turnActive = ev.turn_active === true;
            privacyOn = ev.privacy === true;
            activityText = ev.activity || "";
            break;
        case "error":
            showBanner(ev.message, true);
            break;
        case "notice":
            showBanner(ev.message, false);
            break;
        case "ack": {
            // tag = our own request key (registered before transmission);
            // ref-only events belong to requests that carried no tag.
            var akey = ev.tag || ev.ref;
            if (settleVoiceRef(akey, null))
                break;
            var aentry = akey ? pendingSends[akey] : undefined;
            if (aentry) {
                if (aentry.sent) {
                    // normal order: callback already cleared the composer
                    delete pendingSends[akey];
                    saveDraftNow();
                } else {
                    // ack beat the pyCall callback — park the outcome
                    aentry.outcome = "accepted";
                }
            }
            break;
        }
        case "nack": {
            var nkey = ev.tag || ev.ref;
            var nerror = ev.error || i18n.tr("request failed");
            if (settleVoiceRef(nkey, nerror))
                break;
            var nentry = nkey ? pendingSends[nkey] : undefined;
            if (nentry) {
                if (nentry.sent) {
                    restoreSend(nkey, nerror);
                } else {
                    // composer was never cleared — nothing to restore, the
                    // callback will surface the refusal
                    nentry.outcome = "refused";
                    nentry.error = nerror;
                }
            } else {
                showBanner(nerror, true);
            }
            break;
        }
        case "command_result":
            appendLocalRow("command",
                ev.handled === true
                    ? ((ev.lines || []).join("\n") || i18n.tr("done"))
                    : i18n.tr("Unknown command — try /commands"));
            scrollToEnd();
            break;
        }
    }

    function upsertMessage(ev) {
        if (!ev.id) return;
        var row = {
            mid: String(ev.id),
            role: ev.role || "assistant",
            // Display trim: the CLI sends raw history content, and assistant
            // answers often begin with newlines — which rendered as a block
            // of fake padding at the top of the bubble (field-test finding).
            text: String(ev.text || "").trim(),
            queued: ev.queued === true,
            imagesJson: JSON.stringify(ev.images || []),
            documentsJson: JSON.stringify(ev.documents || []),
            generatedJson: JSON.stringify(ev.generated || []),
            origin: ev.origin || "",
            ts: Number(ev.ts) > 0 ? Number(ev.ts) : 0,
            localRef: "",
            failed: false
        };
        var existing = idIndex[row.mid];
        if (existing !== undefined && existing < chatModel.count) {
            chatModel.set(existing, row);
        } else {
            idIndex[row.mid] = chatModel.count;
            chatModel.append(row);
        }
    }

    function appendLocalRow(role, text, ref) {
        localSerial += 1;
        chatModel.append({
            mid: "local-" + localSerial,
            role: role,
            text: text,
            queued: false,
            imagesJson: "[]",
            documentsJson: "[]",
            generatedJson: "[]",
            origin: "app",
            ts: Date.now() / 1000,
            localRef: ref || "",
            failed: false
        });
    }

    // Voice placeholder lifecycle: ack ⇒ remove (the transcribed user
    // message event follows on its own), nack ⇒ turn the row into a visible
    // failure. Either way the recording file is deleted. Returns whether the
    // ref belonged to a voice request.
    function settleVoiceRef(ref, error) {
        if (!ref || !(ref in voiceFiles)) return false;
        var path = voiceFiles[ref];
        delete voiceFiles[ref];
        app.pyCall("voice_record.delete_recording", [path], null);
        for (var i = chatModel.count - 1; i >= 0; i--) {
            var row = chatModel.get(i);
            if (row.localRef === ref) {
                if (error) {
                    chatModel.setProperty(i, "text",
                        i18n.tr("🎤 Voice message failed: %1").arg(error));
                    chatModel.setProperty(i, "failed", true);
                } else {
                    chatModel.remove(i);
                    reindexFrom(i);
                }
                break;
            }
        }
        return true;
    }

    // chatModel.remove shifts every later row down one — the id map must
    // follow or upserts would edit the wrong bubble.
    function reindexFrom(removedIndex) {
        for (var key in idIndex) {
            if (idIndex[key] > removedIndex) idIndex[key] -= 1;
            else if (idIndex[key] === removedIndex) delete idIndex[key];
        }
    }

    // "10:42" today, "26 Aug 10:42" otherwise — device-local time.
    function formatTime(ts) {
        if (!(ts > 0)) return "";
        var d = new Date(ts * 1000);
        var now = new Date();
        var hm = Qt.formatTime(d, "HH:mm");
        return (d.getFullYear() === now.getFullYear()
                && d.getMonth() === now.getMonth()
                && d.getDate() === now.getDate())
               ? hm
               : Qt.formatDate(d, "d MMM") + " " + hm;
    }

    function scrollToEnd() {
        scrollTimer.restart();
    }
    Timer {
        id: scrollTimer
        interval: 50
        onTriggered: listView.positionViewAtEnd()
    }

    // ------------------------------------------------------------- sending

    function sendCurrent() {
        if (sendInFlight) return;  // double-tap can't submit twice
        // The OSK's predictive engine holds the word being typed as
        // uncommitted preedit — it is NOT in composer.text yet (field
        // finding: sending mid-word dropped the last word). Commit it
        // first so what you see is what is sent.
        Qt.inputMethod.commit();
        var text = composer.text.trim();
        var attachments = pendingAttachments.slice();
        if (text === "" && attachments.length === 0) return;
        if (text.charAt(0) === "/" && attachments.length === 0) {
            appendLocalRow("user", text);
            scrollToEnd();
            sendInFlight = true;
            sendGuardTimer.restart();
            var ctoken = lifeToken;
            app.pyCall("chat_client.send_command", [text], function(r) {
                if (!ctoken.alive) return;
                page.sendInFlight = false;
                if (!r || r.ok !== true) {
                    page.showBanner(r && r.error ? r.error : i18n.tr("not connected to Briglia"), true);
                    return;
                }
                if (DraftLogic.shouldClearComposerText(composer.text, text))
                    composer.text = "";
            });
            return;
        }
        // Register FIRST, persist SECOND, transmit THIRD: the entry exists
        // before Python can start any request (no response ordering can
        // find it missing), and nothing goes on the wire until the on-disk
        // draft mirror is confirmed written — a message that can't be made
        // crash-safe is not sent at all, with the composer untouched.
        var tag = "s" + (++localSerial);
        pendingSends[tag] = { text: text, attachments: attachments,
                              sent: false, cleared: false,
                              outcome: "", error: "" };
        sendInFlight = true;
        sendGuardTimer.restart();
        saveDraftNow(function(saved, saveError) {
            var pre = page.pendingSends[tag];
            if (!pre) { page.sendInFlight = false; return; }  // swept meanwhile — nothing was transmitted
            if (!saved) {
                delete page.pendingSends[tag];
                page.sendInFlight = false;
                page.showBanner(i18n.tr("Could not save the crash-safety draft (%1) — message NOT sent, it's still in the composer").arg(saveError), true);
                return;
            }
            if (pre.outcome !== "") {
                // a disconnect sweep parked a refusal while the draft was
                // being written — nothing was transmitted, composer intact
                delete page.pendingSends[tag];
                page.sendInFlight = false;
                page.saveDraftNow();
                page.showBanner(i18n.tr("Briglia didn't accept the message (%1)").arg(pre.error), true);
                return;
            }
            transmitSend(tag, text, attachments);
        });
    }

    // After a send reached the wire (round 4, Codex): clear the composer
    // text only while it still holds exactly the submitted snapshot —
    // typing that arrived during the async window is never erased. Chips
    // are removed by value, so ones attached since survive. entry.cleared
    // records what actually happened; it drives both the persisted
    // composer_cleared flag and nack/disconnect restoration.
    function clearComposerAfterSend(entry) {
        if (entry.text === "") {
            entry.cleared = true;  // attachment-only: no text to clear or duplicate
        } else if (DraftLogic.shouldClearComposerText(composer.text, entry.text)) {
            composer.text = "";
            entry.cleared = true;
        } else {
            entry.cleared = false;
            showBanner(i18n.tr("Message sent. You kept typing, so the composer wasn't cleared — the already-sent text may still be in it."), false);
        }
        var next = [];
        for (var i = 0; i < pendingAttachments.length; i++)
            if (entry.attachments.indexOf(pendingAttachments[i]) === -1)
                next.push(pendingAttachments[i]);
        pendingAttachments = next;
    }

    function transmitSend(tag, text, attachments) {
        var token = lifeToken;
        app.pyCall("chat_client.send_message", [text, attachments, tag], function(r) {
            if (!token.alive) return;
            page.sendInFlight = false;
            var entry = page.pendingSends[tag];
            if (!entry) return;  // settled by a disconnect sweep meanwhile
            if (!r || r.ok !== true) {
                // Never reached the wire — composer untouched.
                delete page.pendingSends[tag];
                page.saveDraftNow();
                page.showBanner(r && r.error ? r.error : i18n.tr("not connected to Briglia"), true);
                return;
            }
            entry.sent = true;
            if (entry.outcome === "accepted") {
                // ack got here first — done, settle the composer now
                clearComposerAfterSend(entry);
                delete page.pendingSends[tag];
                page.saveDraftNow();
            } else if (entry.outcome === "refused") {
                // nack got here first — composer was never cleared, so
                // there's nothing to restore; just surface the refusal
                var reason = entry.error;
                delete page.pendingSends[tag];
                page.saveDraftNow();
                page.showBanner(i18n.tr("Briglia didn't accept the message (%1)").arg(reason), true);
            } else {
                // normal order: on the wire, awaiting the durable ack
                clearComposerAfterSend(entry);
                page.saveDraftNow();
            }
        });
    }

    // Put a kept send back where the user can act on it: text merged ahead
    // of anything typed since, attachment chips reappearing without dupes.
    // Text comes back only if it was actually cleared (entry.cleared) —
    // an uncleared entry's text never left the composer, so prepending it
    // again would duplicate it (round 4).
    function restoreEntryToComposer(entry) {
        if (entry.cleared === true && entry.text !== "") {
            composer.text = composer.text.trim() === ""
                ? entry.text
                : entry.text + "\n" + composer.text;
        }
        var next = pendingAttachments.slice();
        for (var i = 0; i < entry.attachments.length; i++)
            if (next.indexOf(entry.attachments[i]) === -1)
                next.push(entry.attachments[i]);
        pendingAttachments = next;
    }

    // A send Briglia refused (nack) comes back to the composer instead of
    // disappearing.
    function restoreSend(tag, error) {
        var entry = pendingSends[tag];
        delete pendingSends[tag];
        if (!entry) return;
        restoreEntryToComposer(entry);
        saveDraftNow();
        showBanner(i18n.tr("Briglia didn't accept the message (%1) — it's back in the composer").arg(error), true);
    }

    // Connection lost with unconfirmed requests in flight: their acks can
    // never arrive (refs die with the connection). Sends whose composer was
    // already cleared (`sent`) are restored; ones still awaiting their
    // callback keep the composer as-is and get their refusal parked for the
    // callback. Voice placeholders fail visibly. Honest caveat in the
    // banner: Briglia may still have accepted a send whose ack was lost — the
    // replayed history after reconnect settles it.
    function failUnconfirmedRequests() {
        var hadSends = false;
        for (var tag in pendingSends) {
            var entry = pendingSends[tag];
            if (entry.sent) {
                hadSends = true;
                delete pendingSends[tag];
                restoreEntryToComposer(entry);
            } else if (entry.outcome === "") {
                entry.outcome = "refused";
                entry.error = i18n.tr("connection lost before Briglia confirmed");
            }
        }
        saveDraftNow();
        var voiceTags = [];
        for (var vtag in voiceFiles) voiceTags.push(vtag);
        for (var j = 0; j < voiceTags.length; j++)
            settleVoiceRef(voiceTags[j],
                i18n.tr("connection lost before Briglia confirmed"));
        if (hadSends)
            showBanner(i18n.tr("Connection lost — the unconfirmed message is back in the composer (check the chat after reconnecting: it may still have arrived)"), true);
    }

    // ------------------------------------------------------------- drafts

    // Persist composer + unconfirmed sends so killing the app can't lose a
    // typed message. Immediate on lifecycle changes; typing is debounced
    // through draftTimer. Failures are never silent: `done` (when given)
    // receives (ok, error) so send flow can refuse to transmit, and
    // fire-and-forget saves banner the problem instead of swallowing it.
    function saveDraftNow(done) {
        var payload = DraftLogic.buildDraftPayload(
            composer.text, pendingAttachments, pendingSends);
        var token = lifeToken;
        app.pyCall("chat_client.save_draft",
                   [payload.composer, payload.attachments, payload.pending],
                   function(r) {
            if (!token.alive) return;  // page gone — the save itself still ran
            var ok = r && r.ok === true;
            var error = r && r.error ? r.error : i18n.tr("draft store unavailable");
            if (done) {
                done(ok, error);
            } else if (!ok) {
                page.showBanner(i18n.tr("Draft could not be saved (%1) — a crash right now could lose recent typing").arg(error), true);
            }
        });
    }
    Timer {
        id: draftTimer
        interval: 800
        onTriggered: page.saveDraftNow()
    }

    function restoreDraft() {
        var token = lifeToken;
        app.pyCall("chat_client.load_draft", [], function(d) {
            if (!token.alive) return;
            if (!d || d.ok !== true) return;
            var merged = DraftLogic.mergeDraft(
                d, composer.text, page.pendingAttachments);
            if (merged.text === composer.text
                    && merged.attachments.length === page.pendingAttachments.length
                    && merged.restoredCount === 0
                    && !merged.hadPending)
                return;
            composer.text = merged.text;
            page.pendingAttachments = merged.attachments;
            if (merged.restoredCount > 0)
                page.showBanner(i18n.tr("An unconfirmed message from last time is back in the composer (check the chat: it may still have been delivered)"), true);
            else if (merged.hadPending)
                // The pending record survived with composer_cleared=false:
                // the text restores through the composer copy, but the
                // message may already have reached Briglia (round 4, finding 3
                // — a crash between clearing the UI and persisting the
                // cleared state must not lose the uncertainty warning).
                page.showBanner(i18n.tr("A message was mid-send when the app last closed — it's in the composer; check the chat first: it may already have been delivered"), true);
            // restored entries now live in the composer — collapse the draft
            page.saveDraftNow();
        });
    }

    function addAttachment(path) {
        if (pendingAttachments.indexOf(path) !== -1) return;
        var next = pendingAttachments.slice();
        next.push(path);
        pendingAttachments = next;
        draftTimer.restart();
    }

    function removeAttachment(index) {
        var next = pendingAttachments.slice();
        next.splice(index, 1);
        pendingAttachments = next;
        draftTimer.restart();
    }

    // ------------------------------------------------------------- voice

    function startRecording() {
        var token = lifeToken;
        app.pyCall("voice_record.start_recording", [], function(r) {
            if (!token.alive) return;
            if (!r || r.ok !== true) {
                page.showBanner(r && r.error ? r.error : i18n.tr("could not start recording"), true);
                return;
            }
            page.recordMax = r.max_seconds || 300;
            page.recordSeconds = 0;
            page.recording = true;
        });
    }

    function stopRecordingAndSend() {
        recording = false;
        var token = lifeToken;
        var appRef = app;  // root outlives this page; usable after teardown
        app.pyCall("voice_record.stop_recording", [], function(r) {
            if (!token.alive) {
                // Page gone before the recorder finalized — nobody will ever
                // send or ack this file, so delete it now instead of leaving
                // it to the stale sweep.
                if (r && r.ok === true && r.path)
                    appRef.pyCall("voice_record.delete_recording", [r.path], null);
                return;
            }
            if (!r || r.ok !== true) {
                page.showBanner(r && r.error ? r.error : i18n.tr("recording failed"), true);
                return;
            }
            var path = r.path;
            // Placeholder and file registered under OUR tag before the
            // request exists — an instant ack/nack can't miss them.
            var tag = "v" + (++page.localSerial);
            page.voiceFiles[tag] = path;
            page.appendLocalRow("user", i18n.tr("🎤 Transcribing…"), tag);
            page.scrollToEnd();
            app.pyCall("chat_client.send_voice", [path, tag], function(sr) {
                if (!token.alive) return;  // onDestruction already deletes registered recordings
                if (!sr || sr.ok !== true) {
                    // never reached the wire — settle the placeholder (this
                    // also deletes the recording), unless a disconnect sweep
                    // already did
                    page.settleVoiceRef(tag,
                        sr && sr.error ? sr.error : i18n.tr("not connected to Briglia"));
                }
            });
        });
    }

    function cancelRecording() {
        recording = false;
        app.pyCall("voice_record.cancel_recording", [], null);
    }

    Timer {
        interval: 1000
        running: page.recording
        repeat: true
        onTriggered: {
            page.recordSeconds += 1;
            if (page.recordSeconds >= page.recordMax)
                page.stopRecordingAndSend();
        }
    }

    // ------------------------------------------------------------- lifecycle

    // The event handler is installed at declaration (events with no
    // connection simply never arrive); the socket + draft restore wait for
    // activate() so a pre-setup phone doesn't hammer a socket that cannot
    // exist yet. connect_chat is idempotent and reconnects on its own, so
    // one activation covers the whole app lifetime.
    property bool activated: false
    function activate() {
        if (activated) return;
        activated = true;
        app.pyCall("chat_client.connect_chat", [], null);
        restoreDraft();
    }

    Component.onCompleted: {
        app.setChatHandler(function(ev) { page.handleEvent(ev); });
    }
    Component.onDestruction: {
        // First thing: dead-letter every async callback still in flight.
        // Their Python calls (including the final draft save below) still
        // execute — only their UI reactions are skipped, because after this
        // handler returns the page context can no longer be touched.
        lifeToken.alive = false;
        app.clearChatHandler();
        // The draft is already current (saved on every lifecycle change and
        // debounced typing), but a final synchronous-ish save closes the
        // last keystrokes' 800ms window.
        saveDraftNow();
        app.pyCall("chat_client.disconnect_chat", [], null);
        if (recording)
            app.pyCall("voice_record.cancel_recording", [], null);
        // Recordings whose ack/nack will never be seen (page is going away)
        // must not linger on disk; the recorder's stale sweep is only the
        // backstop for a killed process.
        for (var tag in voiceFiles)
            app.pyCall("voice_record.delete_recording", [voiceFiles[tag]], null);
    }

    // ------------------------------------------------------------- layout

    // status strip at the top of the view (the shell header sits above)
    Rectangle {
        id: statusStrip
        anchors { top: parent.top; left: parent.left; right: parent.right }
        height: Math.max(statusLabel.height + units.gu(1),
                         (stopButton.visible || startButton.visible)
                             ? units.gu(5.5) : 0)
        color: theme.palette.normal.foreground
        Button {
            id: stopButton
            visible: page.turnActive
            anchors { right: parent.right; rightMargin: units.gu(1); verticalCenter: parent.verticalCenter }
            height: units.gu(4)
            color: theme.palette.normal.negative
            text: i18n.tr("■ Stop")
            onClicked: page.app.pyCall("chat_client.send_stop", [], null)
        }
        // Landing on a dead chat must be self-explanatory (design
        // 2026-08-29): daemon down + service installed ⇒ a Start button
        // right in the strip. The chat client's reconnect loop picks the
        // socket up by itself once the daemon is back.
        property bool startWorking: false
        Button {
            id: startButton
            visible: !stopButton.visible
                     && page.connState !== "connected"
                     && page.app.api !== null
                     && page.app.api.daemon_running !== true
                     && page.app.api.service
                     && page.app.api.service.unit_installed === true
            enabled: !statusStrip.startWorking
            anchors { right: parent.right; rightMargin: units.gu(1); verticalCenter: parent.verticalCenter }
            height: units.gu(4)
            color: theme.palette.normal.positive
            text: i18n.tr("Start Briglia")
            onClicked: {
                statusStrip.startWorking = true;
                var token = page.lifeToken;
                page.app.pyCall("systemctl_user", ["start"], function(r) {
                    if (!token.alive) return;
                    statusStrip.startWorking = false;
                    if (!r || r.ok !== true)
                        page.showBanner(page.app.describeError(r), true);
                    page.app.refresh();
                });
            }
        }
        Label {
            id: statusLabel
            anchors {
                left: parent.left
                right: stopButton.visible ? stopButton.left
                       : (startButton.visible ? startButton.left : parent.right)
                verticalCenter: parent.verticalCenter
                margins: units.gu(2)
            }
            textSize: Label.Small
            elide: Text.ElideRight
            color: page.connState === "connected"
                   ? theme.palette.normal.backgroundSecondaryText
                   : theme.palette.normal.negative
            text: {
                if (page.connState !== "connected") {
                    // A daemon that IS running but never accepts predates the
                    // chat socket (CLI < 0.1.45) — say so instead of spinning.
                    if (page.app.api && page.app.api.daemon_running === true)
                        return i18n.tr("Connecting… If this never connects, update Briglia CLI from the Dashboard (chat needs v0.2.0+).");
                    if (page.app.api && page.app.api.daemon_running === false)
                        return i18n.tr("Briglia isn't running on this phone.");
                    return i18n.tr("Connecting to Briglia…");
                }
                if (page.privacyOn)
                    return i18n.tr("Privacy mode is on — messages hidden until /show");
                if (page.turnActive)
                    return page.activityText !== ""
                        ? i18n.tr("Working: %1").arg(page.activityText)
                        : i18n.tr("Working…");
                return i18n.tr("Online");
            }
        }
    }

    // banner for errors / notices
    LomiriShape {
        id: bannerShape
        anchors { top: statusStrip.bottom; left: parent.left; right: parent.right; margins: banner !== "" ? units.gu(1) : 0 }
        visible: page.banner !== ""
        height: visible ? bannerLabel.height + units.gu(2) : 0
        aspect: LomiriShape.Flat
        backgroundColor: page.bannerIsError ? theme.palette.normal.negative
                                            : theme.palette.normal.foreground
        property string banner: page.banner
        Label {
            id: bannerLabel
            anchors { left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter; margins: units.gu(1) }
            wrapMode: Text.WordWrap
            textSize: Label.Small
            color: page.bannerIsError ? "white" : theme.palette.normal.backgroundText
            text: page.banner
        }
        MouseArea { anchors.fill: parent; onClicked: page.banner = "" }
    }

    // message list
    ListView {
        id: listView
        anchors {
            top: bannerShape.visible ? bannerShape.bottom : statusStrip.bottom
            left: parent.left
            right: parent.right
            bottom: composerColumn.top
            margins: units.gu(1)
        }
        clip: true
        spacing: units.gu(1)
        model: chatModel

        delegate: Item {
            width: listView.width
            height: bubble.height

            readonly property bool isUser: model.role === "user"
            readonly property bool isCommand: model.role === "command"
            readonly property var imageNames: JSON.parse(model.imagesJson)
            readonly property var documentNames: JSON.parse(model.documentsJson)
            readonly property var generatedPaths: JSON.parse(model.generatedJson)

            LomiriShape {
                id: bubble
                width: Math.min(bubbleColumn.width + units.gu(3), listView.width - units.gu(2))
                // Column sits gu(1.5) from the top — mirror it below so the
                // padding is symmetric (was +gu(2): 1.5 above, 0.5 below).
                height: bubbleColumn.height + units.gu(3)
                anchors.right: isUser && !isCommand ? parent.right : undefined
                anchors.left: isUser && !isCommand ? undefined : parent.left
                aspect: LomiriShape.Flat
                backgroundColor: {
                    if (model.failed) return theme.palette.normal.negative;
                    if (isCommand) return theme.palette.normal.base;
                    if (isUser) return theme.palette.normal.selection;
                    return theme.palette.normal.foreground;
                }
                opacity: model.queued ? 0.6 : 1.0

                // Underneath the content (declared first, so chips, links
                // and the copy button keep first claim on taps): press and
                // hold anywhere on a bubble to open the message in the
                // full-screen selectable view. A flick becomes a list
                // scroll before the hold matures, so scrolling is unharmed.
                MouseArea {
                    anchors.fill: parent
                    onPressAndHold: {
                        if (model.text !== "")
                            page.app.pushPage("SelectTextPage.qml", {text: model.text});
                    }
                }

                Column {
                    id: bubbleColumn
                    anchors { top: parent.top; left: parent.left; margins: units.gu(1.5) }
                    // A bubble carrying file chips goes full width — the
                    // chips size themselves to their names, and a width
                    // computed only from the text cropped them at the
                    // bubble edge (field finding). Otherwise the width
                    // follows the text/images, with room for the meta row
                    // (time + copy) so they can't overlap on tiny bubbles.
                    width: Math.min(
                        (documentNames.length > 0 || generatedPaths.length > 0)
                        ? listView.width - units.gu(5)
                        : Math.max(bodyLabel.visible ? bodyLabel.implicitWidth : 0,
                                 imageNames.length > 0 ? units.gu(28) : 0,
                                 timeLabel.text !== ""
                                     ? timeLabel.implicitWidth + units.gu(5) : 0,
                                 units.gu(8)),
                        listView.width - units.gu(5))
                    spacing: units.gu(0.5)

                    // non-app origin tag (e.g. a message you sent on Telegram)
                    Label {
                        visible: isUser && model.origin !== "" && model.origin !== "app"
                        textSize: Label.XSmall
                        color: theme.palette.normal.backgroundTertiaryText
                        text: visible ? i18n.tr("via %1").arg(model.origin) : ""
                    }

                    Label {
                        id: bodyLabel
                        visible: model.text !== ""
                        width: Math.min(implicitWidth, listView.width - units.gu(6))
                        wrapMode: Text.Wrap
                        textSize: isCommand ? Label.Small : Label.Medium
                        font.family: isCommand ? "Ubuntu Mono" : "Ubuntu"
                        color: model.failed ? "white" : theme.palette.normal.backgroundText
                        // Assistant replies arrive as markdown (field test:
                        // ** and backticks showed literally) — render the
                        // common subset. User/command text stays plain: what
                        // was typed means exactly what it says.
                        readonly property bool rich: !isUser && !isCommand
                        textFormat: rich ? Text.RichText : Text.PlainText
                        text: rich ? Markdown.toRichText(model.text) : model.text
                        onLinkActivated: Qt.openUrlExternally(link)
                    }

                    // image attachments render inline from the media folder;
                    // tap one for the full-screen viewer
                    Repeater {
                        model: imageNames
                        delegate: Image {
                            source: page.imagesDir !== ""
                                    ? "file://" + page.imagesDir + "/" + modelData : ""
                            width: Math.min(units.gu(28), listView.width - units.gu(8))
                            fillMode: Image.PreserveAspectFit
                            asynchronous: true
                            sourceSize.width: 800
                            MouseArea {
                                anchors.fill: parent
                                onClicked: {
                                    if (page.imagesDir !== "")
                                        page.app.pushPage("ImageViewPage.qml",
                                            {path: page.imagesDir + "/" + modelData});
                                }
                            }
                        }
                    }

                    // documents + agent-generated files as chips — tappable
                    // (field request: the chips were inert labels, so a sent
                    // document could never be opened), underlined as the
                    // affordance; a tap opens the Content Hub's Open-with
                    // dialog with the file.
                    Repeater {
                        model: documentNames
                        delegate: AbstractButton {
                            width: docLabel.width
                            height: docLabel.height + units.gu(1)
                            enabled: page.documentsDir !== ""
                            onClicked: page.app.pushPage("OpenWithPage.qml",
                                {path: page.documentsDir + "/" + modelData})
                            Label {
                                id: docLabel
                                anchors.verticalCenter: parent.verticalCenter
                                width: Math.min(implicitWidth, bubbleColumn.width)
                                elide: Text.ElideMiddle
                                textSize: Label.Small
                                font.underline: parent.enabled
                                color: theme.palette.normal.backgroundSecondaryText
                                text: "📄 " + modelData
                            }
                        }
                    }
                    Repeater {
                        model: generatedPaths
                        delegate: AbstractButton {
                            width: genLabel.width
                            height: genLabel.height + units.gu(1)
                            onClicked: page.app.pushPage("OpenWithPage.qml",
                                {path: modelData})
                            Label {
                                id: genLabel
                                anchors.verticalCenter: parent.verticalCenter
                                width: Math.min(implicitWidth, bubbleColumn.width)
                                elide: Text.ElideMiddle
                                textSize: Label.Small
                                font.underline: true
                                color: theme.palette.normal.backgroundSecondaryText
                                text: "📎 " + modelData
                            }
                        }
                    }

                    Label {
                        visible: model.queued
                        textSize: Label.XSmall
                        color: theme.palette.normal.backgroundTertiaryText
                        text: i18n.tr("queued — Briglia is busy, delivered at the next pause")
                    }

                    // meta row: message time bottom-left, copy-to-clipboard
                    // bottom-right (raw text — for assistant messages that's
                    // the original markdown, not the rendered rich text)
                    Item {
                        visible: model.text !== "" || timeLabel.text !== ""
                        width: bubbleColumn.width
                        height: units.gu(2.5)
                        Label {
                            id: timeLabel
                            anchors { left: parent.left; verticalCenter: parent.verticalCenter }
                            textSize: Label.XSmall
                            color: theme.palette.normal.backgroundTertiaryText
                            text: page.formatTime(model.ts)
                        }
                        AbstractButton {
                            visible: model.text !== ""
                            anchors { right: parent.right; verticalCenter: parent.verticalCenter }
                            width: units.gu(4)
                            height: units.gu(4)
                            onClicked: {
                                Clipboard.push(model.text);
                                page.showBanner(i18n.tr("Copied to clipboard"), false);
                            }
                            Icon {
                                anchors.centerIn: parent
                                width: units.gu(1.75)
                                height: units.gu(1.75)
                                name: "edit-copy"
                                color: theme.palette.normal.backgroundSecondaryText
                            }
                        }
                    }
                }
            }
        }
    }

    // Floating jump-to-latest arrow: visible only while scrolled away from
    // the bottom (with a gu(6) dead zone so it never flickers at rest).
    // Declared after the ListView so it stacks on top of the bubbles.
    AbstractButton {
        id: jumpToLatest
        visible: listView.contentHeight > listView.height
                 && page.distanceFromLatest > units.gu(6)
        anchors { right: listView.right; bottom: listView.bottom; margins: units.gu(1) }
        width: units.gu(5)
        height: units.gu(5)
        onClicked: listView.positionViewAtEnd()
        Rectangle {
            anchors.fill: parent
            radius: width / 2
            color: theme.palette.normal.foreground
            border.color: theme.palette.normal.base
            border.width: units.dp(1)
        }
        Icon {
            anchors.centerIn: parent
            width: units.gu(2.5)
            height: units.gu(2.5)
            name: "go-down"
            color: theme.palette.normal.backgroundText
        }
    }

    // ------------------------------------------------------------- composer
    Column {
        id: composerColumn
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom }
        spacing: 0

        // pending attachment chips
        Flow {
            width: parent.width - units.gu(2)
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: units.gu(0.5)
            visible: page.pendingAttachments.length > 0
            Repeater {
                model: page.pendingAttachments
                delegate: LomiriShape {
                    aspect: LomiriShape.Flat
                    backgroundColor: theme.palette.normal.foreground
                    width: chipLabel.width + units.gu(4)
                    height: units.gu(4)
                    Label {
                        id: chipLabel
                        anchors { left: parent.left; leftMargin: units.gu(1); verticalCenter: parent.verticalCenter }
                        textSize: Label.Small
                        text: "📎 " + String(modelData).split("/").pop()
                    }
                    Label {
                        anchors { right: parent.right; rightMargin: units.gu(1); verticalCenter: parent.verticalCenter }
                        text: "✕"
                        textSize: Label.Small
                    }
                    MouseArea { anchors.fill: parent; onClicked: page.removeAttachment(index) }
                }
            }
        }

        // recording strip replaces the composer while active
        Rectangle {
            width: parent.width
            height: units.gu(7)
            visible: page.recording
            color: theme.palette.normal.foreground
            RowLayout {
                anchors { fill: parent; margins: units.gu(1) }
                spacing: units.gu(1)
                Label {
                    Layout.fillWidth: true
                    text: i18n.tr("🔴 Recording… %1s").arg(page.recordSeconds)
                }
                Button {
                    text: i18n.tr("Cancel")
                    onClicked: page.cancelRecording()
                }
                Button {
                    color: theme.palette.normal.positive
                    text: i18n.tr("Send")
                    onClicked: page.stopRecordingAndSend()
                }
            }
        }

        // Anchor-based, NOT a RowLayout: the layout owned the children's
        // heights and pinned the text field at two rows no matter how much
        // was typed (field-test finding — everything below row two was
        // invisible). With anchors, Lomiri's autoSize is in charge: the
        // field grows with the text up to maximumLineCount rows, then its
        // internal flickable keeps the cursor line in view. The buttons
        // ride the bottom line, messenger-style, as the field grows.
        Item {
            id: composerRow
            width: parent.width - units.gu(2)
            anchors.horizontalCenter: parent.horizontalCenter
            visible: !page.recording
            height: composer.height

            AbstractButton {
                id: attachButton
                width: units.gu(4)
                height: units.gu(4)
                anchors { left: parent.left; bottom: parent.bottom; bottomMargin: units.gu(0.5) }
                onClicked: page.app.pushPage("FilePickerPage.qml", {
                    callback: function(path) { page.addAttachment(path); }
                })
                Icon {
                    anchors.centerIn: parent
                    width: units.gu(2.5); height: units.gu(2.5)
                    name: "attachment"
                }
            }

            TextArea {
                id: composer
                anchors {
                    left: attachButton.right; leftMargin: units.gu(1)
                    right: rightControls.left; rightMargin: units.gu(1)
                    bottom: parent.bottom
                }
                autoSize: true
                maximumLineCount: 6
                placeholderText: page.connState === "connected"
                                 ? i18n.tr("Message Briglia…")
                                 : i18n.tr("Waiting for Briglia…")
                // debounced draft persistence — a killed app keeps the text
                onTextChanged: draftTimer.restart()
            }

            Row {
                id: rightControls
                anchors { right: parent.right; bottom: parent.bottom; bottomMargin: units.gu(0.5) }

                // displayText (text + the keyboard's uncommitted preedit)
                // drives the mic/send swap: with predictive input the first
                // word lives ONLY in preedit until a space/punctuation
                // commits it, so gating on .text kept the mic showing while
                // the user was already typing (field finding).
                AbstractButton {
                    width: units.gu(4)
                    height: units.gu(4)
                    visible: composer.displayText.trim() === "" && page.pendingAttachments.length === 0
                    enabled: page.connState === "connected"
                    onClicked: page.startRecording()
                    Icon {
                        anchors.centerIn: parent
                        width: units.gu(2.5); height: units.gu(2.5)
                        name: "audio-input-microphone-symbolic"
                        opacity: parent.enabled ? 1.0 : 0.4
                    }
                }

                AbstractButton {
                    visible: composer.displayText.trim() !== "" || page.pendingAttachments.length > 0
                    // sendInFlight: one submission at a time — a double-tap on
                    // a slow save/transmit must not send the message twice.
                    enabled: page.connState === "connected" && !page.sendInFlight
                    width: units.gu(4.5)
                    height: units.gu(4.5)
                    opacity: enabled ? 1.0 : 0.5
                    onClicked: page.sendCurrent()
                    Rectangle {
                        anchors.fill: parent
                        radius: width / 2
                        color: theme.palette.normal.positive
                    }
                    Icon {
                        anchors.centerIn: parent
                        width: units.gu(2.25)
                        height: units.gu(2.25)
                        name: "send"
                        color: "white"
                    }
                }
            }
        }

        Item { width: 1; height: units.gu(1) }
    }
}
