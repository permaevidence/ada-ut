// Draft build/merge logic for the chat composer (Codex, 2026-08-28 chat
// rounds 3–4): a message awaiting its send callback exists in BOTH the
// composer and pendingSends — persisting the two naively and merging them
// back duplicated the text after a crash. The rules that fix it live
// here, in one pure file shared by ChatPage.qml and exercised by
// scripts/chat_selftest.py under node (this exact file):
//
//   each pending entry records composer_cleared — whether the composer
//   text was ACTUALLY cleared for it (not merely whether the request
//   reached the wire: round 4 made clearing conditional on the composer
//   still matching the submitted snapshot). Only cleared entries merge
//   their TEXT back on restore; uncleared text is already inside the
//   saved composer copy, so merging it again is the duplication bug.
//   Attachments merge from EVERY entry regardless (round 5): chips leave
//   the composer at wire time even when the text stays, so an uncleared
//   entry can be their only surviving copy — and deduping makes the
//   still-in-composer case harmless.
//
//   hadPending reports whether ANY pending record survived the crash —
//   an uncleared one restores through the composer copy (restoredCount
//   stays 0), but the message may still have reached Briglia, so the
//   delivery-uncertainty warning must key off hadPending, not
//   restoredCount (round 4, finding 3).

// Build the on-disk draft payload from live page state.
// pendingSends: map tag -> {text, attachments, cleared, ...} where
// `cleared` means the composer text was actually cleared for this entry.
function buildDraftPayload(composerText, attachments, pendingSends) {
    var pending = [];
    for (var tag in pendingSends) {
        var entry = pendingSends[tag];
        pending.push({
            text: entry.text || "",
            attachments: (entry.attachments || []).slice(),
            composer_cleared: entry.cleared === true
        });
    }
    return {
        composer: composerText || "",
        attachments: (attachments || []).slice(),
        pending: pending
    };
}

// Merge a loaded draft into the current composer state.
// Returns {text, attachments, restoredCount, hadPending}: text/attachments
// are the new composer content; restoredCount counts pending entries
// actually merged; hadPending is true when any pending record existed at
// all (drives the "may still have been delivered" banner even when the
// text comes back through the composer copy).
function mergeDraft(draft, currentText, currentAttachments) {
    var text = draft.composer || "";
    var atts = (draft.attachments || []).slice();
    var restored = 0;
    var pending = draft.pending || [];
    for (var i = 0; i < pending.length; i++) {
        var entry = pending[i];
        // Attachments merge from EVERY unconfirmed entry (round 5, Codex):
        // the chips are removed from the composer as soon as the send hits
        // the wire even when the text stays (snapshot mismatch), so an
        // uncleared entry can be their only surviving copy. Deduping makes
        // the truly-uncleared case (chips still in the composer list) a
        // no-op. composer_cleared gates only the TEXT — that is what can
        // duplicate through the composer copy.
        var eatts = entry.attachments || [];
        for (var j = 0; j < eatts.length; j++)
            if (atts.indexOf(eatts[j]) === -1)
                atts.push(eatts[j]);
        if (entry.composer_cleared !== true)
            continue;  // its text is already in the composer copy
        restored += 1;
        if (entry.text && entry.text !== "")
            text = text === "" ? entry.text : entry.text + "\n" + text;
    }
    if (text !== "" && (currentText || "").trim() !== "")
        text = text + "\n" + currentText;
    else if (text === "")
        text = currentText || "";
    var merged = (currentAttachments || []).slice();
    for (var k = 0; k < atts.length; k++)
        if (merged.indexOf(atts[k]) === -1)
            merged.push(atts[k]);
    return { text: text, attachments: merged, restoredCount: restored,
             hadPending: pending.length > 0 };
}

// Round 4, finding 1: the send callback can fire after the user has
// already started composing the next message — clearing unconditionally
// erased that typing. The composer text is cleared only while it still
// holds exactly the submitted snapshot; an attachment-only send (empty
// sentText) never clears text.
function shouldClearComposerText(currentText, sentText) {
    return sentText !== "" && (currentText || "").trim() === sentText;
}
