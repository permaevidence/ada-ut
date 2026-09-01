#!/bin/bash
# Stage-8 cutover helper (rename plan §3.3 / §7.2 step 3): move the release
# signing keys on the publishing Mac from the previous identity to the new
# one WITHOUT touching key material.
#
#   ~/.ada-release-keys  →  ~/.briglia-release-keys      (one rename)
#   ada-<c>-release-v1-<fp>.{json,pub.pem,priv.pem}
#       → briglia-<c>-release-v1-<fp>.{json,pub.pem,priv.pem}   (copies; the
#         JSON record's keyId/channel rewritten, everything else byte-identical;
#         the old-named files stay until the post-cutover cleanup, §8)
#   README-RENAME.txt beside each encrypted backup (backup/ inside the key
#   dir and ~/Documents/Ada-Release-Key-Backup) stating the new key IDs —
#   the backups themselves are ID-agnostic raw key material and are NOT
#   modified, re-encrypted or renamed.
#
# Refuses when: both directories exist (a half-done previous run — inspect
# by hand), neither exists, or the previous release watcher is still loaded
# in launchd (it reads the app publication log from the old directory; boot
# it out first — §7.2 step 1). Idempotent: a completed rename is reported
# and exits 0. `--dry-run` prints the plan and changes nothing.
#
# Not touched: the publication log (the publisher writes the new one at the
# first Briglia release), lock files, the Keychain passphrase items, the
# GitHub secret (added by hand as BRIGLIA_CLI_SIGNING_KEY, §3.3).
# Test seam: BRIGLIA_KEYS_HOME overrides $HOME (selftest only).
set -euo pipefail

DRY=0
case "${1:-}" in
    --dry-run) DRY=1;;
    "") ;;
    *) echo "usage: rename-keys-dir.sh [--dry-run]" >&2; exit 64;;
esac

HOME_DIR="${BRIGLIA_KEYS_HOME:-${HOME:?}}"
OLD="$HOME_DIR/.ada-release-keys"
NEW="$HOME_DIR/.briglia-release-keys"
DOCS_BACKUP="$HOME_DIR/Documents/Ada-Release-Key-Backup"
OLD_LABEL_BASE="com.permaevidence.ada-release-watch"
NOTE_NAME="README-RENAME.txt"

say() { echo "$@"; }
act() {  # act <description> <command...> — printed always, executed unless dry-run
    local what="$1"; shift
    if [ "$DRY" = 1 ]; then say "  (dry-run) $what"; return 0; fi
    "$@" || { echo "✖ failed: $what" >&2; exit 1; }
    say "  ✔ $what"
}

twin_name() {  # ada-<c>-release-v1-<fp>[.ext] → briglia-<c>-release-v1-<fp>[.ext]
    local base="$1"
    case "$base" in
        ada-cli-release-v1-*|ada-ut-release-v1-*) echo "briglia-${base#ada-}";;
        *) return 1;;
    esac
}

write_json_twin() {  # <old.json> <new.json>  — keyId/channel rewritten, other fields verbatim
    python3 - "$1" "$2" <<'PYEOF'
import json, os, sys, tempfile
src, dst = sys.argv[1], sys.argv[2]
with open(src) as f:
    data = json.load(f, object_pairs_hook=list)
keys = [k for k, _ in data]
if keys.count("keyId") != 1 or keys.count("channel") != 1:
    sys.exit("✖ %s: not a key record (needs exactly one keyId and one channel)" % src)
out = []
for k, v in data:
    if k == "keyId":
        if not (isinstance(v, str) and v.startswith("ada-")):
            sys.exit("✖ %s: keyId %r is not a previous-identity id" % (src, v))
        v = "briglia-" + v[len("ada-"):]
    elif k == "channel":
        if v not in ("ada-cli", "ada-ut"):
            sys.exit("✖ %s: channel %r is not a previous-identity channel" % (src, v))
        v = "briglia-" + v[len("ada-"):]
    out.append((k, v))
text = json.dumps(dict(out), indent=2) + "\n"
if os.path.exists(dst):
    with open(dst) as f:
        if f.read() == text:
            sys.exit(0)            # already written, identical
    sys.exit("✖ %s exists with different content — inspect by hand" % dst)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst), prefix=".twin-")
with os.fdopen(fd, "w") as f:
    f.write(text)
    f.flush(); os.fsync(f.fileno())
os.chmod(tmp, 0o600)
os.replace(tmp, dst)
PYEOF
}

copy_twin() {  # <src> <dst>  — byte copy, mode 0600, idempotent
    if [ -e "$2" ]; then
        cmp -s "$1" "$2" || { echo "✖ $2 exists with different content — inspect by hand" >&2; exit 1; }
        return 0
    fi
    cp "$1" "$2.tmp" && chmod 600 "$2.tmp" && mv "$2.tmp" "$2"
}

write_note() {  # <dir> <ids...>
    local dir="$1"; shift
    local note="$dir/$NOTE_NAME"
    [ -d "$dir" ] || return 0
    if [ "$DRY" = 1 ]; then say "  (dry-run) write $note"; return 0; fi
    if [ -e "$note" ]; then return 0; fi     # written by a previous run; never rewritten
    CREATED=$((CREATED + 1))
    {
        echo "Release-key rename — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo
        echo "The signing keys did NOT change. Only their IDs and channel names did"
        echo "(the keyId is derived from the channel name plus the public-key fingerprint):"
        echo
        for id in "$@"; do echo "  $id  →  $(twin_name "$id")"; done
        echo
        echo "Channels: ada-cli → briglia-cli, ada-ut → briglia-ut."
        echo "The encrypted backups in this folder hold raw key material and are valid as"
        echo "they are; restore them under the NEW names. Passphrases: the macOS login"
        echo "Keychain items are unchanged (ada-cli-release-key-backup, ada-ut-release-key-backup)."
        echo "Live locations: GitHub secret BRIGLIA_CLI_SIGNING_KEY (briglia-cli), and"
        echo "~/.briglia-release-keys/<new keyId>.priv.pem (briglia-ut)."
    } > "$note.tmp"
    chmod 600 "$note.tmp"
    mv "$note.tmp" "$note"
    say "  ✔ wrote $note"
}

# ---------------------------------------------------------------- preconditions
if [ -d "$NEW" ] && [ ! -e "$OLD" ]; then
    # Directory already moved: only the twin/notes steps remain (each is
    # idempotent: identical → skip, missing → create, different → refuse).
    RESUME=1
elif [ -e "$OLD" ] && [ -e "$NEW" ]; then
    echo "✖ both $OLD and $NEW exist — a previous run stopped half-way; inspect by hand, nothing changed" >&2
    exit 1
elif [ ! -d "$OLD" ]; then
    echo "✖ $OLD does not exist and $NEW does not either — nothing to rename" >&2
    exit 1
else
    RESUME=0
fi

if [ "$(uname)" = "Darwin" ] && command -v launchctl >/dev/null 2>&1; then
    for kind in check heartbeat; do
        if launchctl print "gui/$(id -u)/$OLD_LABEL_BASE.$kind" >/dev/null 2>&1; then
            echo "✖ the previous release watcher ($OLD_LABEL_BASE.$kind) is still loaded — it reads the app" \
                 "publication log from $OLD; boot it out first (cutover step 1), nothing changed" >&2
            exit 1
        fi
    done
fi

SRC_DIR="$OLD"; [ "$RESUME" = 1 ] && SRC_DIR="$NEW"
ids=()
for f in "$SRC_DIR"/ada-*-release-v1-*.json; do
    [ -e "$f" ] || continue
    base="$(basename "$f" .json)"
    twin_name "$base" >/dev/null || { echo "✖ unexpected key record name: $base" >&2; exit 1; }
    ids+=("$base")
done
[ "${#ids[@]}" -gt 0 ] || { echo "✖ no ada-*-release-v1-*.json key records in $SRC_DIR — wrong directory?" >&2; exit 1; }

[ "$RESUME" = 1 ] && say "— $NEW already exists without $OLD: verifying/completing twins and notes only —"
say "— release-key directory rename (plan §3.3) —"
for id in "${ids[@]}"; do say "  $id → $(twin_name "$id")"; done

# ---------------------------------------------------------------- 1. the directory
if [ "$RESUME" = 0 ]; then
    act "rename $OLD → $NEW" mv "$OLD" "$NEW"
fi

# ---------------------------------------------------------------- 2. the twins
CREATED=0
for id in "${ids[@]}"; do
    new_id="$(twin_name "$id")"
    if [ "$DRY" = 1 ]; then
        say "  (dry-run) write $NEW/$new_id.json (keyId/channel rewritten)"
        for ext in pub.pem priv.pem; do [ -e "$SRC_DIR/$id.$ext" ] && say "  (dry-run) copy $id.$ext → $new_id.$ext"; done
        continue
    fi
    if [ -e "$NEW/$new_id.json" ]; then
        write_json_twin "$NEW/$id.json" "$NEW/$new_id.json" || exit 1   # identical → fine, different → refuse
    else
        write_json_twin "$NEW/$id.json" "$NEW/$new_id.json" || exit 1
        say "  ✔ $new_id.json"; CREATED=$((CREATED + 1))
    fi
    for ext in pub.pem priv.pem; do
        [ -e "$NEW/$id.$ext" ] || continue
        if [ -e "$NEW/$new_id.$ext" ]; then
            copy_twin "$NEW/$id.$ext" "$NEW/$new_id.$ext"
        else
            copy_twin "$NEW/$id.$ext" "$NEW/$new_id.$ext"
            say "  ✔ $new_id.$ext"; CREATED=$((CREATED + 1))
        fi
    done
done

# ---------------------------------------------------------------- 3. backup notes
write_note "$NEW/backup" "${ids[@]}"
write_note "$DOCS_BACKUP" "${ids[@]}"

if [ "$DRY" = 1 ]; then
    say "dry-run complete — nothing changed"
elif [ "$RESUME" = 1 ] && [ "$CREATED" = 0 ]; then
    say "✔ already renamed: $NEW complete (no $OLD); nothing to do"
elif [ "$RESUME" = 1 ]; then
    say "✔ completed the missing twins/notes ($CREATED) in $NEW. Old-named files kept until the post-cutover cleanup (§8)."
else
    say "✔ done. Old-named files kept in $NEW until the post-cutover cleanup (§8)."
fi
