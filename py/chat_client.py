"""JSON-lines client for Briglia CLI's companion-app chat socket.

The Briglia daemon serves a chat front-end protocol on a Unix domain socket
(`~/.local/share/briglia/app-chat.sock`, briglia-cli docs/UT_CHAT_PLAN.md): the app
connects, receives a `hello` with a history snapshot plus live `message` /
`status` / `error` events, and submits turns with `send` / `voice` / `stop` /
`command` requests, each correlated by a client `ref`.

This module owns the connection on a background thread and forwards every
server event to QML verbatim via pyotherside.send("chat-event", event).
Connection lifecycle is reported through synthetic {"type": "_connection"}
events so the page can render connecting/offline states. Reconnection is
automatic with capped backoff while the chat is open — the daemon restarting
(e.g. after /upgrade) just looks like a brief "connecting…" spell.

Nothing here is Briglia-state-aware: the module is a dumb pipe by design, so the
protocol lives in exactly two places (CLI server, QML page) instead of three.
"""

import itertools
import json
import os
import socket
import threading
import time

try:
    import pyotherside
except ImportError:  # unit-testing off-device
    class _NullSide:
        @staticmethod
        def send(*args):
            pass
    pyotherside = _NullSide()


def socket_path():
    """Resolved lazily so tests can retarget via the environment."""
    return os.environ.get(
        "BRIGLIA_CHAT_SOCKET",
        os.path.expanduser("~/.local/share/briglia/app-chat.sock"))


def draft_path():
    """Composer-draft store; resolved lazily for the same test seam reason."""
    return os.environ.get(
        "BRIGLIA_UT_DRAFT_PATH",
        os.path.expanduser("~/.cache/briglia.permaevidence/chat-draft.json"))


# Seam for tests: replaced to capture the event stream.
def _emit(event):
    pyotherside.send("chat-event", event)


_lock = threading.Lock()
_write_lock = threading.Lock()
_state = {
    "enabled": False,
    "sock": None,
    "status": "disconnected",
    # Bumped on every connect/disconnect: a loop from an older generation
    # must exit instead of fighting the new one for the socket.
    "generation": 0,
}
_ref_counter = itertools.count(1)
# Wire ref -> caller tag, registered BEFORE the request bytes leave (under
# _lock, in the sending thread) so the reader thread can never see a response
# whose tag mapping doesn't exist yet. This is the fix for the ack race: QML
# registers its request under a tag it generated synchronously, hands the tag
# to send_*, and every ack/nack comes back carrying that tag — the pyCall
# return callback stops being part of the correctness path entirely.
_ref_tags = {}

RECONNECT_MAX_BACKOFF = 5.0


def _set_status(status, detail=""):
    with _lock:
        _state["status"] = status
    _emit({"type": "_connection", "state": status, "detail": detail})


def _loop_should_exit(generation):
    with _lock:
        return not _state["enabled"] or _state["generation"] != generation


def _reader_loop(generation):
    backoff = 0.5
    while True:
        if _loop_should_exit(generation):
            return
        _set_status("connecting")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(5)
            sock.connect(socket_path())
            sock.settimeout(None)
        except OSError as exc:
            try:
                sock.close()
            except OSError:
                pass
            _set_status("disconnected", str(exc))
            deadline = time.time() + backoff
            while time.time() < deadline:
                if _loop_should_exit(generation):
                    return
                time.sleep(0.2)
            backoff = min(backoff * 2, RECONNECT_MAX_BACKOFF)
            continue

        with _lock:
            if not _state["enabled"] or _state["generation"] != generation:
                try:
                    sock.close()
                except OSError:
                    pass
                return
            _state["sock"] = sock
        _set_status("connected")
        backoff = 0.5

        buf = bytearray()
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf.extend(chunk)
                while True:
                    nl = buf.find(b"\n")
                    if nl == -1:
                        break
                    line = bytes(buf[:nl])
                    del buf[:nl + 1]
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line.decode("utf-8", "replace"))
                    except ValueError:
                        continue  # never let one bad line kill the stream
                    if isinstance(event, dict):
                        ref = event.get("ref")
                        if ref is not None:
                            with _lock:
                                tag = _ref_tags.get(ref)
                                # ack/nack settle the request — the mapping
                                # is done; pong/command_result keep theirs
                                # (unused today, harmless).
                                if tag is not None and event.get("type") in (
                                        "ack", "nack"):
                                    del _ref_tags[ref]
                            if tag is not None:
                                event = dict(event)
                                event["tag"] = tag
                        _emit(event)
        except OSError:
            pass

        with _lock:
            if _state["sock"] is sock:
                _state["sock"] = None
            # Refs die with the connection — their acks can never arrive.
            # QML treats the _connection event as the settle-everything
            # signal; stale mappings must not leak onto a future ref reuse.
            _ref_tags.clear()
        try:
            sock.close()
        except OSError:
            pass
        if _loop_should_exit(generation):
            return
        _set_status("disconnected", "connection lost")
        # brief pause before the reconnect attempt
        deadline = time.time() + 0.5
        while time.time() < deadline:
            if _loop_should_exit(generation):
                return
            time.sleep(0.1)


# ---------------------------------------------------------------- public API

def connect_chat():
    """Open (or keep) the background connection; events start flowing to the
    "chat-event" handler. Idempotent."""
    with _lock:
        if _state["enabled"]:
            return {"ok": True, "already_connected": True}
        _state["enabled"] = True
        _state["generation"] += 1
        generation = _state["generation"]
    threading.Thread(
        target=_reader_loop, args=(generation,), daemon=True).start()
    return {"ok": True}


def disconnect_chat():
    """Stop reconnecting and drop the current connection."""
    with _lock:
        _state["enabled"] = False
        _state["generation"] += 1
        sock = _state["sock"]
        _state["sock"] = None
        _ref_tags.clear()
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
    _set_status("disconnected", "closed")
    return {"ok": True}


def connection_state():
    with _lock:
        return _state["status"]


def _send(request, tag=None):
    with _lock:
        sock = _state["sock"]
    if sock is None:
        return {"ok": False, "error": "not connected to Briglia"}
    request = dict(request)
    ref = "q%d" % next(_ref_counter)
    request["ref"] = ref
    if tag is not None:
        # Registered before a single byte hits the wire: the reader thread
        # can then never see this ref's response without its tag.
        with _lock:
            _ref_tags[ref] = str(tag)
    data = (json.dumps(request) + "\n").encode("utf-8")
    try:
        with _write_lock:
            sock.sendall(data)
    except OSError as exc:
        if tag is not None:
            with _lock:
                _ref_tags.pop(ref, None)
        return {"ok": False, "error": "send failed: %s" % exc}
    return {"ok": True, "ref": ref, "tag": tag}


def send_message(text, attachments=None, tag=None):
    return _send({"type": "send", "text": text or "",
                  "attachments": list(attachments or [])}, tag=tag)


def send_voice(path, tag=None):
    return _send({"type": "voice", "path": path}, tag=tag)


def send_stop():
    return _send({"type": "stop"})


def send_command(line):
    return _send({"type": "command", "line": line})


def ping():
    return _send({"type": "ping"})


# ---------------------------------------------------------------- draft store

# Draft file format version. Absent in files written by app 0.4.2/0.4.3;
# 0.4.2 additionally stored pending entries WITHOUT composer_cleared, which
# load_draft migrates (Codex round 4, finding 2).
DRAFT_FORMAT = 2


def save_draft(composer_text, attachments=None, pending=None):
    """Persist the composer + unconfirmed sends so killing the app cannot
    lose a typed message. All-empty state deletes the file (nothing worth
    keeping, nothing stale to restore). Atomic via os.replace.

    Each pending entry carries composer_cleared: False means the composer
    still holds that text (it is ALSO inside the saved composer field), True
    means the composer was cleared after transmission and the entry is the
    only surviving copy — the restore path merges only the latter, which is
    what prevents Codex round 3's duplicate restoration."""
    data = {
        "format": DRAFT_FORMAT,
        "composer": composer_text or "",
        "attachments": [str(a) for a in (attachments or [])],
        "pending": [
            {"text": str(p.get("text", "")),
             "attachments": [str(a) for a in (p.get("attachments") or [])],
             "composer_cleared": bool(p.get("composer_cleared"))}
            for p in (pending or []) if isinstance(p, dict)
        ],
    }
    path = draft_path()
    if not data["composer"] and not data["attachments"] and not data["pending"]:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            # An undeletable stale draft WILL be restored next launch —
            # claiming success here would hide that. Only "already gone"
            # counts as cleared.
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "cleared": True}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


def load_draft():
    """Read back a persisted draft; malformed or absent files come back as
    the empty draft, never an exception."""
    empty = {"ok": True, "composer": "", "attachments": [], "pending": []}
    try:
        with open(draft_path()) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    result = dict(empty)
    if isinstance(data.get("composer"), str):
        result["composer"] = data["composer"]
    if isinstance(data.get("attachments"), list):
        result["attachments"] = [str(a) for a in data["attachments"]]
    if isinstance(data.get("pending"), list):
        # Migration (Codex round 4): app 0.4.2 wrote pending entries without
        # composer_cleared and no format field. Defaulting the missing flag
        # to False silently DROPPED a normally-cleared in-flight message on
        # restore. For legacy files the safe default is True (restore —
        # losing text is worse than a visible duplicate), except when the
        # entry's exact text still sits inside the saved composer: that is
        # the uncleared case, where restoring WOULD duplicate it. Format-2
        # files always carry the flag explicitly, so no guessing happens.
        legacy = "format" not in data
        pending = []
        for p in data["pending"]:
            if not isinstance(p, dict):
                continue
            text = str(p.get("text", ""))
            cleared = p.get("composer_cleared")
            if cleared is None and legacy:
                cleared = not (text != "" and text in result["composer"])
            pending.append({
                "text": text,
                "attachments": [str(a) for a in (p.get("attachments") or [])],
                "composer_cleared": bool(cleared),
            })
        result["pending"] = pending
    return result
