// Ownership logic for bundle-scanned values in text fields (Codex,
// 2026-08-28 round 3): a field is "bundle-owned" only while its text
// still EQUALS the exact value last injected into it. Boolean flags are
// not enough — they both left a stale key in place when a second bundle
// replaced the entry, and erased a hand-edited value on discard.
//
// Pure function, shared by ProviderPage/KeysPage/IdentityPage/Telegram
// and exercised by scripts/qr_selftest.py under node (same file).
//
// sync(fieldText, injected, scanned) -> {text, injected}
//   fieldText: current field content
//   injected:  the exact value this field last received from the bundle
//              ("" = none)
//   scanned:   the bundle's current value for this field's key
//              (undefined/"" = the entry is not in the bundle)
// Rules:
//   - bundle-owned text (=== injected) follows the bundle: replaced when
//     the entry changes, cleared when the entry leaves (discard/save/
//     remove/wizard end);
//   - an empty field accepts injection;
//   - user-owned text (anything else) is never touched, and ownership
//     lapses once the entry leaves the bundle.

function sync(fieldText, injected, scanned) {
    var text = fieldText;
    if (!scanned) {
        if (injected !== "" && text === injected)
            text = "";
        return { text: text, injected: "" };
    }
    if (text === "" || text === injected)
        return { text: scanned, injected: scanned };
    return { text: text, injected: injected };
}
