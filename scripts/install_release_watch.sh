#!/bin/bash
# Install (or refresh) the release-channel watcher on this Mac as two
# launchd user agents (RELEASE_SIGNING_PLAN.md §10, Phase E):
#
#   com.permaevidence.ada-release-watch.check      hourly   → release_watch.py check
#   com.permaevidence.ada-release-watch.heartbeat  hourly   → release_watch.py heartbeat
#
# The watcher runs from a SNAPSHOT under ~/.config/ada-release-watch/bin
# (release_watch.py + py/release_verify.py copied from this checkout and
# recorded with the git commit), never from the working tree — editing the
# repo must not silently change what the monitor does. Re-run this script
# to deploy a new snapshot; it prints the diff of what changed.
#
#   scripts/install_release_watch.sh            # install / refresh
#   scripts/install_release_watch.sh --uninstall
#
# Alerts go to the Telegram chat configured in the env file named by the
# watcher config (default ~/.claude/channels/telegram/.env — the same bot
# Claude Code uses on this Mac). Logs: ~/Library/Logs/ada-release-watch/.
set -euo pipefail
cd "$(dirname "$0")/.."

LABEL_BASE="com.permaevidence.ada-release-watch"
HOME_DIR="${HOME:?}"
ROOT="$HOME_DIR/.config/ada-release-watch"
BIN="$ROOT/bin"
LOGS="$HOME_DIR/Library/Logs/ada-release-watch"
AGENTS="$HOME_DIR/Library/LaunchAgents"
CONFIG="$ROOT/config.json"
PY="$(command -v python3)"

unload() {
    for kind in check heartbeat; do
        launchctl bootout "gui/$(id -u)/$LABEL_BASE.$kind" 2>/dev/null || true
    done
}

if [ "${1:-}" = "--uninstall" ]; then
    unload
    rm -f "$AGENTS/$LABEL_BASE.check.plist" "$AGENTS/$LABEL_BASE.heartbeat.plist"
    echo "✔ agents removed (state kept in $ROOT — delete it yourself if you mean it)"
    exit 0
fi

[ "$(uname)" = "Darwin" ] || { echo "✖ launchd installer is macOS-only (cron/systemd elsewhere)"; exit 1; }
[ -f scripts/release_watch.py ] && [ -f py/release_verify.py ] || { echo "✖ run from an ada-ut checkout"; exit 1; }

# --- snapshot (diffed against the previous one)
mkdir -p "$BIN/py" "$LOGS"
chmod 700 "$ROOT"
for pair in "scripts/release_watch.py:$BIN/release_watch.py" "py/release_verify.py:$BIN/py/release_verify.py"; do
    src="${pair%%:*}"; dst="${pair#*:}"
    if [ -f "$dst" ] && ! cmp -s "$src" "$dst"; then
        echo "— $src changed since the installed snapshot:"
        diff -u "$dst" "$src" | head -40 || true
    fi
    install -m 0644 "$src" "$dst"
done
git rev-parse HEAD > "$BIN/SNAPSHOT_COMMIT" 2>/dev/null || echo unknown > "$BIN/SNAPSHOT_COMMIT"
"$PY" -m py_compile "$BIN/release_watch.py" "$BIN/py/release_verify.py"

# --- config: production defaults live in the script; the file only pins
# the state dir and can carry overrides (e.g. a transition manifest).
if [ ! -f "$CONFIG" ]; then
    cat > "$CONFIG" <<EOF
{
  "state_dir": "$ROOT",
  "channels": {}
}
EOF
    chmod 600 "$CONFIG"
    echo "✔ wrote $CONFIG"
fi

# --- launchd agents
plist() {  # label kind minute
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$1</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>$BIN/release_watch.py</string><string>$2</string>
    <string>--config</string><string>$CONFIG</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Minute</key><integer>$3</integer></dict>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$LOGS/$2.log</string>
  <key>StandardErrorPath</key><string>$LOGS/$2.log</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
</dict></plist>
EOF
}
unload
mkdir -p "$AGENTS"
plist "$LABEL_BASE.check" check 7 > "$AGENTS/$LABEL_BASE.check.plist"
plist "$LABEL_BASE.heartbeat" heartbeat 37 > "$AGENTS/$LABEL_BASE.heartbeat.plist"
for kind in check heartbeat; do
    plutil -lint -s "$AGENTS/$LABEL_BASE.$kind.plist"
    launchctl bootstrap "gui/$(id -u)" "$AGENTS/$LABEL_BASE.$kind.plist"
done
echo "✔ installed: check at :07 every hour, heartbeat at :37 every hour (both ran once now — RunAtLoad)"
echo "  snapshot $(cat "$BIN/SNAPSHOT_COMMIT" | cut -c1-12) in $BIN, logs in $LOGS"
echo "  status:   launchctl print gui/$(id -u)/$LABEL_BASE.check | grep -E 'state|last exit'"
echo "  state:    $PY $BIN/release_watch.py status --config $CONFIG"
