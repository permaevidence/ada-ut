#!/usr/bin/env python3
"""Independent heartbeat for the release-channel watcher (RELEASE_SIGNING_PLAN.md §10).

Alerts a human over Telegram when `release_watch.py check` has stopped
completing. It is deliberately DISJOINT from the checker so that whatever
breaks the checker cannot also silence the report about it:

  * standard library only — it never imports release_watch.py or
    py/release_verify.py (a missing/broken verifier module is one of the
    failures it must report);
  * it reads ONE input from the checker: the completion beacon
    `<state_dir>/check.beacon.json`, a tiny JSON file the checker writes
    atomically at the end of every completed run. It never opens the
    checker's `state.json` or takes the checker's lock — a corrupt state
    file or a wedged/long-running `check` are failures to report, not
    reasons to fail;
  * it keeps its own bookkeeping in `<state_dir>/heartbeat-state.json`
    under its own lock (`heartbeat.lock`). If its own state file is
    unreadable it still alerts — with an empty memory and a note — rather
    than exit;
  * the config file is optional and tolerated: a missing or corrupt config
    falls back to the built-in defaults and is itself mentioned in the
    alert (only `state_dir`, `telegram_env_file`, `telegram_api`,
    `heartbeat_max_age_hours`, `realert_hours` are read).

The one thing it shares with the checker is the Telegram bot credential
file — an unavoidable common dependency for anything that has to talk to
you; a missing credential file is reported on stderr and by the exit code.

    release_heartbeat.py [--config PATH] [--status]

Alert conditions: no beacon (the check has never completed), unreadable or
implausible beacon, beacon older than `heartbeat_max_age_hours`, or the
checker reporting undelivered alerts queued for longer than that window.
Policy as the checker's: first occurrence, re-alert every `realert_hours`,
one recovery message; undelivered messages are queued and retried.
Exit 0 = healthy, 2 = alerting, 1 = the heartbeat itself could not run.
"""

import argparse
import datetime
import fcntl
import json
import os
import sys
import time
import urllib.error
import urllib.request

HEARTBEAT_VERSION = "1"
USER_AGENT = "ada-release-heartbeat/" + HEARTBEAT_VERSION
BEACON_NAME = "check.beacon.json"
STATE_NAME = "heartbeat-state.json"
LOCK_NAME = "heartbeat.lock"
FUTURE_SLACK = 600          # a beacon "completed" more than 10 min in the future is corrupt

DEFAULTS = {
    "state_dir": "~/.config/ada-release-watch",
    "telegram_env_file": "~/.claude/channels/telegram/.env",
    "telegram_api": "https://api.telegram.org",
    "heartbeat_max_age_hours": 3,
    "realert_hours": 6,
}


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ config

def load_config(path):
    """Defaults overlaid with the file; returns (cfg, problem_or_None).
    Never raises: the heartbeat must run with a broken config."""
    cfg = dict(DEFAULTS)
    if not path:
        return cfg, None
    try:
        with open(os.path.expanduser(path)) as f:
            user = json.load(f)
        if not isinstance(user, dict):
            raise ValueError("top level is not an object")
    except Exception as exc:  # noqa: BLE001
        return cfg, "config %s unreadable (%s: %s) — using built-in defaults" % (path, type(exc).__name__, exc)
    for k in DEFAULTS:
        if k in user:
            cfg[k] = user[k]
    try:
        float(cfg["heartbeat_max_age_hours"]); float(cfg["realert_hours"])
        str(cfg["state_dir"]); str(cfg["telegram_env_file"]); str(cfg["telegram_api"])
    except Exception as exc:  # noqa: BLE001
        return dict(DEFAULTS), "config %s has invalid values (%s) — using built-in defaults" % (path, exc)
    return cfg, None


# ---------------------------------------------------------------- telegram

def telegram_credentials(cfg):
    path = os.path.expanduser(cfg["telegram_env_file"])
    token = chat = None
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
            elif line.startswith("OWNER_CHAT_ID="):
                chat = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not token or not chat:
        raise RuntimeError("telegram env file %s lacks TELEGRAM_BOT_TOKEN / OWNER_CHAT_ID" % path)
    return token, chat


def send_telegram(cfg, text):
    """True when Telegram confirmed delivery. Never raises; never logs the token."""
    token = None
    try:
        token, chat = telegram_credentials(cfg)
        body = json.dumps({"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(cfg["telegram_api"] + "/bot" + token + "/sendMessage", data=body, method="POST",
                                     headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status, raw = resp.status, resp.read(65536)
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, (exc.read(65536) if exc.fp else b"")
        return status == 200 and json.loads(raw.decode("utf-8", "replace")).get("ok") is True
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if token:
            msg = msg.replace(token, "[TOKEN]")
        print("  ! telegram delivery failed: %s" % msg, file=sys.stderr)
        return False


# ------------------------------------------------------------------- state

def atomic_write_json(path, data):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def load_own_state(path):
    """(state, problem_or_None). Unreadable own state → empty memory + note."""
    empty = {"version": HEARTBEAT_VERSION, "active": None, "queued": []}
    if not os.path.exists(path):
        return empty, None
    try:
        with open(path) as f:
            st = json.load(f)
        if not isinstance(st, dict) or not isinstance(st.get("queued", []), list):
            raise ValueError("unexpected shape")
        if st.get("active") is not None and not isinstance(st["active"], dict):
            raise ValueError("unexpected shape")
        st.setdefault("queued", [])
        st.setdefault("active", None)
        st["queued"] = [m for m in st["queued"] if isinstance(m, str)]
        return st, None
    except Exception as exc:  # noqa: BLE001
        return empty, "own state %s unreadable (%s) — continuing with empty memory" % (path, type(exc).__name__)


# ------------------------------------------------------------------ beacon

def read_beacon(state_dir, now, max_age):
    """(problem_or_None, beacon_or_None). problem is the alert text."""
    path = os.path.join(state_dir, BEACON_NAME)
    if not os.path.exists(path):
        return "the hourly check has NEVER completed (no %s in %s)" % (BEACON_NAME, state_dir), None
    try:
        with open(path) as f:
            b = json.load(f)
        completed = float(b["completed"])
    except Exception as exc:  # noqa: BLE001
        return "the check's completion beacon %s is unreadable (%s) — the checker may be crashing mid-run" % (path, type(exc).__name__), None
    if completed > now + FUTURE_SLACK:
        return "the completion beacon claims a time in the future (%s) — clock or corruption problem" % iso(completed), b
    age = now - completed
    if age > max_age:
        return ("the hourly check has not completed since %s (%.1f h ago, limit %.0f h). "
                "The watcher itself may be broken or the Mac may be offline." % (iso(completed), age / 3600, max_age / 3600)), b
    queued = b.get("queued") or 0
    oldest = b.get("oldest_queued")
    if isinstance(queued, int) and queued > 0 and isinstance(oldest, (int, float)) and now - oldest > max_age:
        return ("the check completes but has %d undelivered alert(s) queued since %s — the checker cannot reach Telegram; "
                "read ~/Library/Logs/ada-release-watch/check.log" % (queued, iso(oldest))), b
    return None, b


# ---------------------------------------------------------------------- run

def run(cfg, cfg_problem, now=None):
    now = time.time() if now is None else now
    state_dir = os.path.expanduser(cfg["state_dir"])
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    lock_fd = os.open(os.path.join(state_dir, LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        print("✖ another heartbeat run holds %s" % os.path.join(state_dir, LOCK_NAME))
        return 1
    try:
        state_path = os.path.join(state_dir, STATE_NAME)
        st, state_problem = load_own_state(state_path)
        max_age = float(cfg["heartbeat_max_age_hours"]) * 3600
        realert = float(cfg["realert_hours"]) * 3600
        problem, beacon = read_beacon(state_dir, now, max_age)
        notes = [n for n in (cfg_problem, state_problem) if n]
        messages = []
        if problem:
            text = problem + ("".join("\n(note: %s)" % n for n in notes))
            prev = st.get("active")
            if prev is None:
                st["active"] = {"first": now, "last_sent": now, "text": problem}
                messages.append("🚨 ada release heartbeat — %s" % text)
            elif now - float(prev.get("last_sent", 0)) >= realert or prev.get("text") != problem:
                prev["last_sent"] = now
                prev["text"] = problem
                messages.append("🚨 ada release heartbeat — STILL FAILING since %s — %s" % (iso(float(prev.get("first", now))), text))
            print("  ✖ %s" % problem)
        else:
            prev = st.get("active")
            if prev:
                st["active"] = None
                messages.append("✅ ada release heartbeat — hourly checks are completing again (last %s; failing since %s)"
                                % (iso(float(beacon["completed"])), iso(float(prev.get("first", now)))))
            print("  ✔ last check completed %s (%d finding(s), %d queued)"
                  % (iso(float(beacon["completed"])), int(beacon.get("findings") or 0), int(beacon.get("queued") or 0)))
        for n in notes:
            print("  ! %s" % n, file=sys.stderr)
        pending = list(st["queued"]) + messages
        st["queued"] = []
        for m in pending:
            if not send_telegram(cfg, m):
                st["queued"].append(m)
        st["last_run"] = now
        st["version"] = HEARTBEAT_VERSION
        try:
            atomic_write_json(state_path, st)
        except Exception as exc:  # noqa: BLE001 — the alert (if any) was already attempted; say so and go on
            print("  ! could not persist heartbeat state: %s" % exc, file=sys.stderr)
        print("heartbeat: %s — %d message(s) sent, %d queued" % ("ALERT" if problem else "ok", len(pending) - len(st["queued"]), len(st["queued"])))
        return 2 if problem else 0
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", help="watcher config JSON (optional; tolerated when unreadable)")
    ap.add_argument("--status", action="store_true", help="print the beacon and the heartbeat's own state")
    args = ap.parse_args(argv)
    cfg, cfg_problem = load_config(args.config)
    print("ada release heartbeat v%s — %s" % (HEARTBEAT_VERSION, iso(time.time())))
    if args.status:
        state_dir = os.path.expanduser(cfg["state_dir"])
        for name in (BEACON_NAME, STATE_NAME):
            p = os.path.join(state_dir, name)
            print("— %s —" % p)
            try:
                print(open(p).read().rstrip())
            except Exception as exc:  # noqa: BLE001
                print("(unreadable: %s)" % exc)
        if cfg_problem:
            print("! " + cfg_problem)
        return 0
    return run(cfg, cfg_problem)


if __name__ == "__main__":
    sys.exit(main())
