#!/usr/bin/env python3
"""Offline selftest for the chat arc: py/chat_client.py against a scripted
fake socket server (connect / hello passthrough / request shapes /
reconnect / disconnect), py/voice_record.py against fake recorder binaries
on PATH (gstreamer + arecord flavors, INT-finalize, empty-capture and
instant-death failures, path-jail deletion), and structural sanity for the
new QML pages (brace balance, no QtQuick.Controls import — the BusyIndicator
lesson — and a declared-type whitelist).

Runs on any POSIX dev box: no device, no Qt, no network.
Usage: python3 scripts/chat_selftest.py
"""

import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Short socket dir (sockaddr_un cap) + isolated recording dir, both set
# BEFORE the modules are imported (they freeze paths at import).
WORK = tempfile.mkdtemp(prefix="briglia-ut-chat-", dir="/tmp")
os.environ["BRIGLIA_CHAT_SOCKET"] = os.path.join(WORK, "chat.sock")
os.environ["BRIGLIA_UT_VOICE_DIR"] = os.path.join(WORK, "voice")
os.environ["BRIGLIA_UT_DRAFT_PATH"] = os.path.join(WORK, "drafts", "chat-draft.json")

sys.path.insert(0, os.path.join(ROOT, "py"))
import chat_client  # noqa: E402
import voice_record  # noqa: E402

PASSED = FAILED = 0


def check(label, ok, detail=""):
    global PASSED, FAILED
    print("%s %s%s" % ("PASS" if ok else "FAIL", label,
                       "" if ok or not detail else " — " + detail))
    if ok:
        PASSED += 1
    else:
        FAILED += 1


# ---------------------------------------------------------------- fake server

class FakeServer:
    """Scripted app-chat server: accepts one client at a time, greets with a
    hello, records every request line, answers pings."""

    def __init__(self, path):
        self.path = path
        self.requests = []
        self.conn = None
        self.listener = None
        self.lock = threading.Lock()
        self.hello = {"type": "hello", "protocol": 1, "version": "test",
                      "images_dir": "/tmp/img", "documents_dir": "/tmp/doc",
                      "privacy": False, "turn_active": False,
                      "status": "idle", "history": []}

    def start(self):
        if os.path.exists(self.path):
            os.unlink(self.path)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        self.listener.listen(2)
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        listener = self.listener
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with self.lock:
                self.conn = conn
            self.send(self.hello)
            threading.Thread(target=self._read_loop, args=(conn,),
                             daemon=True).start()

    def _read_loop(self, conn):
        buf = bytearray()
        while True:
            try:
                chunk = conn.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buf.extend(chunk)
            while True:
                nl = buf.find(b"\n")
                if nl == -1:
                    break
                line = bytes(buf[:nl])
                del buf[:nl + 1]
                try:
                    req = json.loads(line)
                except ValueError:
                    continue
                with self.lock:
                    self.requests.append(req)
                if req.get("type") == "ping":
                    self.send({"type": "pong", "ref": req.get("ref")})

    def send(self, obj):
        with self.lock:
            conn = self.conn
        if conn is None:
            return
        try:
            conn.sendall((json.dumps(obj) + "\n").encode())
        except OSError:
            pass

    def send_raw(self, data):
        with self.lock:
            conn = self.conn
        if conn is not None:
            try:
                conn.sendall(data)
            except OSError:
                pass

    def drop_client(self):
        with self.lock:
            conn = self.conn
            self.conn = None
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()

    def stop(self):
        listener = self.listener
        self.listener = None
        if listener is not None:
            listener.close()
        self.drop_client()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def wait_request(self, ref, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                for req in self.requests:
                    if req.get("ref") == ref:
                        return req
            time.sleep(0.05)
        return None


# ---------------------------------------------------------------- event capture

EVENTS = []
EVENTS_LOCK = threading.Lock()


def capture_emit(event):
    with EVENTS_LOCK:
        EVENTS.append(event)


def wait_event(predicate, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with EVENTS_LOCK:
            for ev in EVENTS:
                if predicate(ev):
                    return ev
        time.sleep(0.05)
    return None


def clear_events():
    with EVENTS_LOCK:
        del EVENTS[:]


def run_client_tests():
    chat_client._emit = capture_emit
    sock_path = os.environ["BRIGLIA_CHAT_SOCKET"]

    # connect with no server: state goes connecting → disconnected, and
    # sends are honestly refused
    result = chat_client.connect_chat()
    check("client: connect_chat starts the loop", result.get("ok") is True)
    check("client: no server → disconnected state event",
          wait_event(lambda e: e.get("type") == "_connection"
                     and e.get("state") == "disconnected") is not None)
    refused = chat_client.send_message("hi", [])
    check("client: send while disconnected is refused",
          refused.get("ok") is False and "not connected" in refused.get("error", ""))

    # server appears: the standing retry loop connects and forwards hello
    server = FakeServer(sock_path)
    server.start()
    check("client: reconnect loop finds a late-starting server",
          wait_event(lambda e: e.get("type") == "_connection"
                     and e.get("state") == "connected") is not None)
    hello = wait_event(lambda e: e.get("type") == "hello")
    check("client: hello forwarded verbatim",
          hello is not None and hello.get("images_dir") == "/tmp/img"
          and hello.get("protocol") == 1)

    # request shapes + ref correlation
    sent = chat_client.send_message("ciao ada", ["/tmp/a.txt", "/tmp/b.pdf"])
    req = server.wait_request(sent.get("ref"))
    check("client: send_message shape (text + attachments + ref)",
          req is not None and req.get("type") == "send"
          and req.get("text") == "ciao ada"
          and req.get("attachments") == ["/tmp/a.txt", "/tmp/b.pdf"])

    sent = chat_client.send_voice("/tmp/v.ogg")
    req = server.wait_request(sent.get("ref"))
    check("client: send_voice shape",
          req is not None and req.get("type") == "voice"
          and req.get("path") == "/tmp/v.ogg")

    sent = chat_client.send_command("/status")
    req = server.wait_request(sent.get("ref"))
    check("client: send_command shape",
          req is not None and req.get("type") == "command"
          and req.get("line") == "/status")

    sent = chat_client.send_stop()
    req = server.wait_request(sent.get("ref"))
    check("client: send_stop shape", req is not None and req.get("type") == "stop")

    sent = chat_client.ping()
    pong = wait_event(lambda e: e.get("type") == "pong"
                      and e.get("ref") == sent.get("ref"))
    check("client: ping → pong round trip", pong is not None)

    # tag round trip (the Codex round-2 ack-race fix): the ref→tag mapping
    # is registered before transmission, so the enriched ack can never miss
    # it — QML keys its request bookkeeping on the tag it generated itself
    clear_events()
    sent = chat_client.send_message("tagged hello", [], tag="s42")
    check("client: send_message returns the caller's tag",
          sent.get("ok") is True and sent.get("tag") == "s42")
    req = server.wait_request(sent.get("ref"))
    check("client: tag never crosses the wire",
          req is not None and "tag" not in req)
    server.send({"type": "ack", "ref": sent.get("ref")})
    ack = wait_event(lambda e: e.get("type") == "ack"
                     and e.get("ref") == sent.get("ref"))
    check("client: ack comes back enriched with the tag",
          ack is not None and ack.get("tag") == "s42")
    # the ack settled the mapping — a duplicate response is tagless
    clear_events()
    server.send({"type": "ack", "ref": sent.get("ref")})
    dup = wait_event(lambda e: e.get("type") == "ack"
                     and e.get("ref") == sent.get("ref"))
    check("client: settled ref no longer maps to a tag",
          dup is not None and "tag" not in dup)

    # nack path + voice tags behave the same
    clear_events()
    sent = chat_client.send_voice("/tmp/v.ogg", tag="v7")
    server.wait_request(sent.get("ref"))
    server.send({"type": "nack", "ref": sent.get("ref"), "error": "no key"})
    nack = wait_event(lambda e: e.get("type") == "nack"
                      and e.get("ref") == sent.get("ref"))
    check("client: voice nack carries the tag",
          nack is not None and nack.get("tag") == "v7"
          and nack.get("error") == "no key")

    # draft store: register→save→load round trip (composer_cleared state
    # preserved per entry), all-empty clears the file, a corrupt file
    # degrades to the empty draft
    saved = chat_client.save_draft(
        "typed so far", ["/tmp/pic.jpg"],
        [{"text": "unconfirmed msg", "attachments": ["/tmp/doc.pdf"],
          "composer_cleared": True},
         {"text": "still in composer", "attachments": []}])
    loaded = chat_client.load_draft()
    check("draft: save/load round trip preserves composer_cleared",
          saved.get("ok") is True and loaded.get("ok") is True
          and loaded.get("composer") == "typed so far"
          and loaded.get("attachments") == ["/tmp/pic.jpg"]
          and loaded.get("pending") == [
              {"text": "unconfirmed msg", "attachments": ["/tmp/doc.pdf"],
               "composer_cleared": True},
              {"text": "still in composer", "attachments": [],
               "composer_cleared": False}])
    chat_client.save_draft("", [], [])
    check("draft: all-empty save deletes the file",
          not os.path.exists(chat_client.draft_path())
          and chat_client.load_draft().get("composer") == "")
    os.makedirs(os.path.dirname(chat_client.draft_path()), exist_ok=True)
    with open(chat_client.draft_path(), "w") as f:
        f.write("{corrupt")
    corrupt = chat_client.load_draft()
    check("draft: corrupt file degrades to the empty draft",
          corrupt.get("ok") is True and corrupt.get("composer") == ""
          and corrupt.get("pending") == [])
    os.remove(chat_client.draft_path())

    # deletion honesty (Codex round 3): a stale draft that CANNOT be removed
    # will be restored next launch — claiming "cleared" hides that. A
    # directory at the draft path makes os.remove fail even for root.
    os.makedirs(chat_client.draft_path(), exist_ok=True)
    undeletable = chat_client.save_draft("", [], [])
    check("draft: failed clear reports the error instead of success",
          undeletable.get("ok") is False and undeletable.get("error", "") != "")
    check("draft: unreadable path degrades load to the empty draft",
          chat_client.load_draft().get("composer") == "")
    os.rmdir(chat_client.draft_path())
    check("draft: absent file still clears successfully",
          chat_client.save_draft("", [], []).get("ok") is True)

    # format versioning + 0.4.2 migration (Codex round 4): old files carry
    # no format field and no composer_cleared. A normally-cleared in-flight
    # entry must RESTORE (True), while one whose exact text still sits in
    # the saved composer must stay skipped (restoring would duplicate it).
    # New files declare format 2, and their missing flags never migrate.
    chat_client.save_draft("x", [], [])
    with open(chat_client.draft_path()) as f:
        check("draft: saves declare format 2",
              json.load(f).get("format") == chat_client.DRAFT_FORMAT == 2)
    with open(chat_client.draft_path(), "w") as f:
        json.dump({"composer": "", "attachments": [],
                   "pending": [{"text": "sent then crashed",
                                "attachments": ["/tmp/v.pdf"]}]}, f)
    mig = chat_client.load_draft()
    check("draft: legacy 0.4.2 cleared-composer entry migrates to restorable",
          mig.get("pending") == [{"text": "sent then crashed",
                                  "attachments": ["/tmp/v.pdf"],
                                  "composer_cleared": True}], str(mig))
    with open(chat_client.draft_path(), "w") as f:
        json.dump({"composer": "still here", "attachments": [],
                   "pending": [{"text": "still here", "attachments": []}]}, f)
    mig2 = chat_client.load_draft()
    check("draft: legacy entry whose text is still in the composer stays skipped",
          mig2.get("pending")[0].get("composer_cleared") is False, str(mig2))
    with open(chat_client.draft_path(), "w") as f:
        # same shape the legacy rule would migrate to True — the format
        # field alone must keep it False
        json.dump({"format": 2, "composer": "", "attachments": [],
                   "pending": [{"text": "gone", "attachments": []}]}, f)
    check("draft: format-2 files never migrate a missing flag",
          chat_client.load_draft().get("pending")[0].get("composer_cleared")
          is False)
    os.remove(chat_client.draft_path())

    # a garbage line from the server must not kill the stream
    clear_events()
    server.send_raw(b"not json at all\n")
    server.send({"type": "status", "turn_active": True, "status": "working"})
    check("client: malformed server line skipped, stream continues",
          wait_event(lambda e: e.get("type") == "status"
                     and e.get("turn_active") is True) is not None)

    # server drops the connection: client reports it and reconnects
    clear_events()
    server.drop_client()
    check("client: drop → disconnected event",
          wait_event(lambda e: e.get("type") == "_connection"
                     and e.get("state") == "disconnected") is not None)
    check("client: automatic reconnect after a drop",
          wait_event(lambda e: e.get("type") == "_connection"
                     and e.get("state") == "connected", 15) is not None)
    check("client: fresh hello after reconnect",
          wait_event(lambda e: e.get("type") == "hello", 10) is not None)

    # disconnect_chat: closed for good — no reconnect even with the server up
    clear_events()
    chat_client.disconnect_chat()
    check("client: disconnect reports closed",
          wait_event(lambda e: e.get("type") == "_connection"
                     and e.get("state") == "disconnected") is not None)
    time.sleep(1.2)
    clear_events()
    time.sleep(1.5)
    with EVENTS_LOCK:
        reconnected = any(e.get("type") == "_connection"
                          and e.get("state") == "connected" for e in EVENTS)
    check("client: no reconnect after explicit disconnect", not reconnected)
    check("client: connection_state reports disconnected",
          chat_client.connection_state() == "disconnected")

    server.stop()


# ---------------------------------------------------------------- voice tests

FAKE_GST = """#!/bin/sh
f=""
for a in "$@"; do case "$a" in location=*) f="${a#location=}";; esac; done
: > "$f"
trap 'dd if=/dev/zero bs=200 count=3 >> "$f" 2>/dev/null; exit 0' INT
while :; do sleep 0.05; done
"""

FAKE_GST_EMPTY = """#!/bin/sh
f=""
for a in "$@"; do case "$a" in location=*) f="${a#location=}";; esac; done
: > "$f"
trap 'exit 0' INT
while :; do sleep 0.05; done
"""

FAKE_GST_DEAD = """#!/bin/sh
echo "no pulseaudio daemon" >&2
exit 1
"""

FAKE_ARECORD = """#!/bin/sh
f=""
for a in "$@"; do f="$a"; done
: > "$f"
trap 'dd if=/dev/zero bs=200 count=3 >> "$f" 2>/dev/null; exit 0' INT
while :; do sleep 0.05; done
"""

# Deterministic stand-in for coreutils timeout (absent on a stock Mac):
# argv is always ["timeout", "-s", "INT", "-k", "10", "<secs>", cmd...] —
# strip the five options and exec the recorder. The real hard-cap semantics
# belong to coreutils; what our code owns (building that argv) is pinned by
# the _full_argv test below.
FAKE_TIMEOUT = """#!/bin/sh
shift 5
exec "$@"
"""


def install_fake(bin_dir, name, body):
    path = os.path.join(bin_dir, name)
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    # Warm the script once: macOS security scanning can delay the FIRST exec
    # of a freshly written script by whole seconds, which flakes every
    # timing-sensitive check downstream (a "dead" recorder that hasn't even
    # started 0.4s in, a SIGINT probe whose trap isn't installed yet). One
    # throwaway spawn absorbs the scan before anything measures.
    try:
        proc = subprocess.Popen(
            [path], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return path
    time.sleep(0.1)
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    return path


def run_voice_tests():
    original_path = os.environ["PATH"]
    bin_dir = os.path.join(WORK, "fakebin")
    os.makedirs(bin_dir, exist_ok=True)

    try:
        # no recorder at all: PATH reduced to the (empty) fake dir
        os.environ["PATH"] = bin_dir
        info = voice_record.backend_info()
        check("voice: missing recorders → readable error",
              info.get("ok") is False and "recorder" in info.get("error", ""))

        # Functional phases: fakes shadow any real recorder, but the fake
        # scripts still need /bin (sleep, dd) on their own PATH. A fake
        # timeout makes the hard-cap wrapper deterministic on every dev box.
        os.environ["PATH"] = bin_dir + os.pathsep + original_path
        install_fake(bin_dir, "timeout", FAKE_TIMEOUT)

        # the recorder argv is wrapped in the suspension-proof hard cap:
        # SIGINT (finalizes the container), -k escalation, cap+grace seconds
        full = voice_record._full_argv("gstreamer", "/tmp/x.ogg")
        check("voice: hard-cap wrapper — timeout -s INT -k precedes the recorder",
              full[:5] == ["timeout", "-s", "INT", "-k", "10"]
              and full[5] == str(voice_record.MAX_SECONDS
                                 + voice_record.HARD_CAP_GRACE)
              and full[6] == "gst-launch-1.0",
              str(full[:7]))

        # ---- signal-independent checks (run in every environment)

        # deletion is jailed to the recording dir
        os.makedirs(voice_record.RECORD_DIR, exist_ok=True)
        outside = os.path.join(WORK, "outside.ogg")
        with open(outside, "w") as f:
            f.write("x")
        check("voice: delete refuses paths outside the recording dir",
              voice_record.delete_recording(outside).get("ok") is False
              and os.path.exists(outside))
        finished = os.path.join(voice_record.RECORD_DIR, "voice-done.ogg")
        with open(finished, "wb") as f:
            f.write(b"x" * 600)
        check("voice: delete removes a finished note",
              voice_record.delete_recording(finished).get("ok") is True
              and not os.path.exists(finished))
        check("voice: stop with no recording is refused",
              voice_record.stop_recording().get("ok") is False)

        # stale sweep as a pure function: orphans past the cutoff go, fresh
        # files stay
        stale = os.path.join(voice_record.RECORD_DIR, "voice-orphan.ogg")
        fresh = os.path.join(voice_record.RECORD_DIR, "voice-fresh.ogg")
        for f, age in ((stale, voice_record.STALE_AFTER_SECONDS + 60), (fresh, 60)):
            with open(f, "w") as fh:
                fh.write("x" * 200)
            past = time.time() - age
            os.utime(f, (past, past))
        voice_record._sweep_stale_files()
        check("voice: stale sweep removes orphans, keeps fresh notes",
              not os.path.exists(stale) and os.path.exists(fresh))
        os.remove(fresh)

        # recorder dying instantly surfaces its stderr (no signals involved).
        # Production judges "instantly dead" 0.4s after spawn — under heavy
        # load (or a lingering first-exec scan) the child may not even have
        # run by then, making start look healthy. That's benign in
        # production (stop then reports an empty recording); here it's a
        # flake, so retry the scenario a couple of times, cancelling the
        # false-healthy start so state can't leak into later checks.
        install_fake(bin_dir, "gst-launch-1.0", FAKE_GST_DEAD)
        dead = None
        for _ in range(3):
            dead = voice_record.start_recording()
            if dead.get("ok") is not True:
                break
            voice_record.cancel_recording()
            time.sleep(0.5)
        check("voice: instantly-dead recorder → stderr in the error",
              dead.get("ok") is False and "pulseaudio" in dead.get("error", ""),
              str(dead))

        # ---- finalize/watchdog checks need SIGINT to actually reach child
        # processes. Some review sandboxes (Codex's macOS seatbelt) block
        # that, failing checks that pass everywhere else and on-device —
        # probe once and SKIP loudly instead of failing on the environment.
        if not _sigint_deliverable(bin_dir):
            print("SKIP voice: this environment cannot deliver SIGINT to "
                  "child processes — 8 finalize/watchdog checks skipped "
                  "(they pass on unsandboxed machines; the Pixel field test "
                  "is the real validation)")
            return

        # gstreamer flavor: start → stop produces a finalized file
        install_fake(bin_dir, "gst-launch-1.0", FAKE_GST)
        started = voice_record.start_recording()
        check("voice: gstreamer start",
              started.get("ok") is True and started.get("backend") == "gstreamer"
              and started.get("path", "").endswith(".ogg"),
              str(started))
        check("voice: double start refused",
              voice_record.start_recording().get("ok") is False)
        time.sleep(0.3)
        stopped = voice_record.stop_recording()
        check("voice: stop finalizes a non-empty file",
              stopped.get("ok") is True and stopped.get("size", 0) > 128
              and os.path.exists(stopped.get("path", "")),
              str(stopped))
        if stopped.get("ok"):
            voice_record.delete_recording(stopped["path"])

        # cancel removes the partial file
        started = voice_record.start_recording()
        cancelled = voice_record.cancel_recording()
        check("voice: cancel removes the partial recording",
              cancelled.get("ok") is True and cancelled.get("was_recording") is True
              and not os.path.exists(started.get("path", "/nonexistent")))

        # empty capture is an error, not a 0-byte send
        install_fake(bin_dir, "gst-launch-1.0", FAKE_GST_EMPTY)
        voice_record.start_recording()
        time.sleep(0.2)
        empty = voice_record.stop_recording()
        check("voice: empty capture → readable error",
              empty.get("ok") is False and "empty" in empty.get("error", ""))

        # Python watchdog (layer 2): a recording the UI never stops is
        # finalized at cap+2s even with no stop/cancel call — and a later
        # stop still returns the finished file instead of an error.
        install_fake(bin_dir, "gst-launch-1.0", FAKE_GST)
        voice_record.MAX_SECONDS = 1
        try:
            started = voice_record.start_recording()
            proc = voice_record._current["proc"]
            deadline = time.time() + 8
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.2)
            check("voice: watchdog finalizes an unstopped recording",
                  proc.poll() is not None, "recorder still running after cap")
            late_stop = voice_record.stop_recording()
            check("voice: stop after watchdog finalize still returns the file",
                  late_stop.get("ok") is True
                  and os.path.exists(late_stop.get("path", "")),
                  str(late_stop))
            if late_stop.get("ok"):
                voice_record.delete_recording(late_stop["path"])
        finally:
            voice_record.MAX_SECONDS = 300

        # arecord fallback. With the gst fake removed, a REAL gst-launch-1.0
        # on this dev box would win backend selection and record actual
        # audio — skip loudly there instead of testing the wrong thing.
        os.remove(os.path.join(bin_dir, "gst-launch-1.0"))
        if shutil.which("gst-launch-1.0", path=original_path):
            print("SKIP voice: arecord fallback (real gst-launch-1.0 present)")
        else:
            install_fake(bin_dir, "arecord", FAKE_ARECORD)
            started = voice_record.start_recording()
            check("voice: arecord fallback produces a wav",
                  started.get("ok") is True and started.get("backend") == "arecord"
                  and started.get("path", "").endswith(".wav"),
                  str(started))
            time.sleep(0.2)
            stopped = voice_record.stop_recording()
            check("voice: arecord stop finalizes", stopped.get("ok") is True,
                  str(stopped))
            if stopped.get("ok"):
                voice_record.delete_recording(stopped["path"])
    finally:
        os.environ["PATH"] = original_path


def _sigint_deliverable(bin_dir):
    """Whether this environment lets us SIGINT a child and have its trap
    run. Review sandboxes can block it; the recorder finalize path (and its
    fakes) fundamentally depend on it."""
    probe = install_fake(
        bin_dir, "sigprobe",
        "#!/bin/sh\ntrap 'echo GOT; exit 0' INT\nwhile :; do sleep 0.05; done\n")
    try:
        proc = subprocess.Popen([probe], stdout=subprocess.PIPE)
    except OSError:
        return False
    time.sleep(0.4)
    try:
        proc.send_signal(signal.SIGINT)
    except OSError:
        proc.kill()
        return False
    try:
        out, _ = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return False
    return b"GOT" in out


# ---------------------------------------------------------------- QML sanity

# Types provided by the modules the pages import. The BusyIndicator crash
# (v0.3.4) happened because a QtQuick.Controls type slipped in unnoticed —
# this whitelist makes a foreign type a test failure on the dev box instead
# of a crash on the phone.
KNOWN_TYPES = {
    # QtQuick / QtQuick.Layouts
    "Item", "Rectangle", "Column", "Row", "Flow", "Image", "Text", "Timer",
    "ListView", "ListModel", "ListElement", "Repeater", "MouseArea",
    "Component", "Connections", "ColumnLayout", "RowLayout", "GridLayout",
    "Layout", "Qt", "QtObject", "State", "Transition", "Behavior",
    "NumberAnimation", "FontMetrics", "Flickable", "Loader",
    # Lomiri.Components
    "MainView", "Page", "PageHeader", "PageStack", "Label", "Button",
    "TextField", "TextArea", "Switch", "CheckBox", "ActivityIndicator",
    "ProgressBar", "Icon", "LomiriShape", "ListItem", "AbstractButton",
    "Action", "OptionSelector", "Slider", "Sections",
    # Lomiri.Components.Popups
    "Dialog", "PopupUtils",
    # pyotherside
    "Python",
    # QtMultimedia (ScanPage)
    "Camera", "VideoOutput",
    # Lomiri.Content (OpenWithPage — Content Hub file handover)
    "ContentPeerPicker", "ContentItem",
}

TYPE_RE = re.compile(r"^\s*([A-Z][A-Za-z0-9_]*)\s*\{")

# One-pass strings-and-comments stripper. A naive comment-first pass
# truncates "file://" INSIDE a string (// looks like a comment) and the
# resulting unpaired quote swallows whole regions — the alternation tries
# the string branch first at each position, so // inside a string is safe.
STRIP_RE = re.compile(
    r'"(?:[^"\\\n]|\\.)*"'      # double-quoted string (single line)
    r"|'(?:[^'\\\n]|\\.)*'"     # single-quoted string
    r"|//[^\n]*"                # line comment
    r"|/\*.*?\*/",              # block comment
    re.S)


def strip_qml(source):
    return STRIP_RE.sub(
        lambda m: '""' if m.group(0)[0] in "\"'" else "", source)


def run_qml_checks():
    qml_files = []
    for base, _, files in os.walk(os.path.join(ROOT, "qml")):
        for name in files:
            if name.endswith(".qml"):
                qml_files.append(os.path.join(base, name))
    check("qml: chat pages present",
          any(f.endswith("ChatPage.qml") for f in qml_files)
          and any(f.endswith("FilePickerPage.qml") for f in qml_files))

    # Components in this repo's qml tree resolve by file name.
    local_types = {os.path.splitext(os.path.basename(f))[0] for f in qml_files}

    for path in sorted(qml_files):
        rel = os.path.relpath(path, ROOT)
        with open(path) as f:
            source = f.read()
        stripped = strip_qml(source)
        balanced = (stripped.count("{") == stripped.count("}")
                    and stripped.count("(") == stripped.count(")")
                    and stripped.count("[") == stripped.count("]"))
        check("qml balance: %s" % rel, balanced,
              "{%d/%d (%d/%d [%d/%d" % (
                  stripped.count("{"), stripped.count("}"),
                  stripped.count("("), stripped.count(")"),
                  stripped.count("["), stripped.count("]")))
        check("qml: %s does not import QtQuick.Controls" % rel,
              "QtQuick.Controls" not in source)
        unknown = sorted({
            m.group(1) for m in map(TYPE_RE.match, stripped.splitlines()) if m
        } - KNOWN_TYPES - local_types)
        check("qml types resolve: %s" % rel, not unknown, ", ".join(unknown))

    # Field-test regressions (Pixel, 2026-08-28). Structural pins on the
    # exact lines that fixed them, so a refactor can't silently drop one.
    with open(os.path.join(ROOT, "qml", "Main.qml")) as f:
        main_src = f.read()
    # 1. OSK covered the chat composer: MainView must resize for the
    #    on-screen keyboard.
    check("qml pin: MainView anchors to the on-screen keyboard",
          "anchorToKeyboard: true" in main_src)
    # 2. A late pyCall callback from a destroyed page threw into
    #    PyOtherSide's onError and painted a fake fatal "startup problem".
    #    Backstop: pyCall must contain the try/catch around cb(result).
    check("qml pin: pyCall callbacks run inside try/catch",
          "try {" in main_src and "cb(result);" in main_src)
    # Shell redesign (2026-08-29, design settled with the owner): a single
    # shell page carries Chat / Dashboard / Settings as header sections
    # over always-alive views (visibility switching — the chat socket and
    # composer must survive tab hops), launch routes to setup until the
    # wizard is complete and to the shell (Chat default) after.
    # Round 2 (field feedback): the strip is sections-ONLY — a PageHeader
    # title row stole vertical space while repeating the highlighted tab.
    check("qml pin: shell strip is sections-only, no title header",
          "Sections {" in main_src
          and 'i18n.tr("Chat"), i18n.tr("Dashboard"), i18n.tr("Settings")'
              in main_src
          and "extension: Sections" not in main_src
          and "shellHeader" not in main_src)
    # Round 3 (field, 2026-08-29): the strip must be the Page's `header`,
    # not a plain child — MainView keeps an internal legacy AppHeader
    # alive for any page with `header` unset, hidden at launch but
    # re-exposed on app-focus events (empty white band above the tabs
    # after the first prompt).
    check("qml pin: shell strip is the Page header (legacy AppHeader off)",
          "header: Rectangle {" in main_src
          and main_src.index("header: Rectangle {")
              < main_src.index("id: sectionsBar"))
    check("qml pin: shell views switch by visibility, not push/pop",
          main_src.count("shellSections.selectedIndex ===") >= 4
          and "ChatPage {" in main_src
          and "DashboardPage {" in main_src
          and "SettingsPage {" in main_src)
    check("qml pin: launch routes — wizard until complete, shell after",
          "function routeInitial()" in main_src
          and "gotoShell();" in main_src
          and "startWizard();" in main_src
          and "root.refresh(function() { root.routeInitial(); });" in main_src)
    check("qml pin: routing never yanks an open wizard/install page",
          "if (stack.currentPage !== bootPage) return;" in main_src)
    with open(os.path.join(ROOT, "qml", "pages", "ChatPage.qml")) as f:
        chat_src = f.read()
    # ...and the root fix: ChatPage callbacks capture a plain-JS life token
    #    that Component.onDestruction flips before the final draft save.
    check("qml pin: ChatPage declares lifeToken",
          "property var lifeToken" in chat_src)
    check("qml pin: ChatPage.onDestruction dead-letters callbacks first",
          "lifeToken.alive = false" in chat_src)
    check("qml pin: ChatPage async callbacks guard on the life token",
          strip_qml(chat_src).count(".alive) ") >= 7)
    # 3. Assistant bubbles rendered leading newlines from raw history
    #    content as fake top padding: display text must be trimmed.
    check("qml pin: ChatPage trims bubble text for display",
          'String(ev.text || "").trim()' in chat_src)
    # Shell redesign: chat is an embedded always-alive view. The socket
    # connects on activate() (guarded, idempotent), never at declaration —
    # a pre-setup phone must not spin a doomed reconnect loop during the
    # wizard. And a dead chat must be actionable: daemon down + service
    # installed shows a Start button in the status strip.
    check("qml pin: chat connects on guarded activate(), not at declaration",
          "function activate()" in chat_src
          and "if (activated) return;" in chat_src
          and "connect_chat" not in
              chat_src.split("Component.onCompleted:")[1]
                      .split("Component.onDestruction:")[0])
    check("qml pin: chat offers Start Briglia when the daemon is down",
          "startButton" in chat_src
          and '"systemctl_user", ["start"]' in chat_src)
    # 4. The composer stayed two rows tall — RowLayout owned the field's
    #    height and defeated autoSize. The composer must stay anchor-based
    #    (the only RowLayout left is the recording strip) and grow to 6
    #    rows.
    check("qml pin: composer is anchor-based so autoSize owns its height",
          chat_src.count("RowLayout {") == 1
          and "maximumLineCount: 6" in chat_src)
    # 5. Assistant bubbles render markdown via the shared pure file; user
    #    and command text stays plain.
    check("qml pin: assistant bubbles render markdown, user text plain",
          'import "../MarkdownLogic.js" as Markdown' in chat_src
          and "rich ? Text.RichText : Text.PlainText" in chat_src
          and "rich ? Markdown.toRichText(model.text) : model.text"
              in chat_src)
    # 6. Field request (2026-08-28): copy button on every text bubble,
    #    press-and-hold opens the selectable view, and the document /
    #    generated-file chips + photos are tappable (chips were inert
    #    labels — the "can't open the documents I sent" bug).
    check("qml pin: bubbles have a copy-to-clipboard button",
          "Clipboard.push(model.text)" in chat_src)
    check("qml pin: press-and-hold opens the selectable text view",
          "onPressAndHold" in chat_src and "SelectTextPage.qml" in chat_src)
    check("qml pin: document chips open via the hub Open-with page",
          "OpenWithPage.qml" in chat_src
          and 'page.documentsDir + "/" + modelData' in chat_src)
    check("qml pin: photos open the full-screen viewer",
          "ImageViewPage.qml" in chat_src
          and 'page.imagesDir + "/" + modelData}' in chat_src)
    with open(os.path.join(ROOT, "qml", "pages", "OpenWithPage.qml")) as f:
        open_src = f.read()
    check("qml pin: OpenWithPage exports as a hub Destination, pops on cancel",
          "ContentHandler.Destination" in open_src
          and "ContentTransfer.Charged" in open_src
          and "onCancelPressed" in open_src
          and '"file://" + page.path' in open_src)
    with open(os.path.join(ROOT, "qml", "pages", "SelectTextPage.qml")) as f:
        sel_src = f.read()
    check("qml pin: SelectTextPage is a read-only selectable TextArea",
          "readOnly: true" in sel_src
          and "autoSize: false" in sel_src
          and "Clipboard.push(page.text)" in sel_src)
    # 7. Field round 2 (2026-08-28): Lomiri's draggable selection handles
    #    are gated on cursorVisible, which Qt forces false on read-only
    #    fields — long-press selected one word and could never extend.
    #    The page must pin cursorVisible back after every internal write.
    check("qml pin: SelectTextPage reasserts cursorVisible for the handles",
          "function keepHandlesVisible()" in sel_src
          and "onCursorVisibleChanged: keepHandlesVisible()" in sel_src
          and "onActiveFocusChanged: keepHandlesVisible()" in sel_src
          and "onSelectedTextChanged: keepHandlesVisible()" in sel_src)
    check("qml pin: SelectTextPage offers Select all",
          "selectAll()" in sel_src and "edit-select-all" in sel_src)
    #    ...and the chat gets a floating jump-to-latest arrow, with
    #    auto-follow suspended while the user reads older history (the
    #    nearEnd capture must happen BEFORE the upsert).
    check("qml pin: floating jump-to-latest arrow over the list",
          '"go-down"' in chat_src
          and chat_src.count("positionViewAtEnd()") >= 2)
    check("qml pin: live messages only auto-scroll when already near the end",
          "var nearEnd = page.distanceFromLatest < units.gu(10)" in chat_src
          and "if (nearEnd) scrollToEnd();" in chat_src)
    # 9. Field round 4 (2026-08-28): the arrow never showed because the
    #    distance ignored ListView's drifting content origin. Every
    #    distance-from-bottom computation must go through the originY-
    #    aware property.
    check("qml pin: distance-from-latest accounts for ListView originY",
          "listView.originY + listView.contentHeight" in chat_src
          and "page.distanceFromLatest > units.gu(6)" in chat_src)
    #    Send visibility must see the OSK's uncommitted preedit
    #    (displayText), and sending must commit it first — gating on
    #    .text alone hid the send button until a space committed the
    #    first word, and sending mid-word would drop the word.
    check("qml pin: mic/send swap watches displayText (preedit-aware)",
          'composer.displayText.trim() === ""' in chat_src
          and 'composer.displayText.trim() !== ""' in chat_src
          and "Qt.inputMethod.commit();" in chat_src)
    check("qml pin: stop is a red button in the status strip",
          "theme.palette.normal.negative" in chat_src
          and "stopButton" in chat_src
          and "media-playback-stop" not in chat_src)
    check("qml pin: send is a green circular arrow (voice strip keeps text)",
          'name: "send"' in chat_src
          and chat_src.count("theme.palette.normal.positive") >= 2)
    # 8. Field round 3 (2026-08-28): chips overflowed the bubble (width
    #    computed only from text/images), and messages had no visible
    #    time. Bubbles with file chips go full width, chips clamp to the
    #    column, and every bubble shows the event's ts bottom-left (both
    #    model paths must carry ts — the server has sent it all along).
    check("qml pin: bubbles with file chips take the full width",
          "documentNames.length > 0 || generatedPaths.length > 0" in chat_src
          and chat_src.count("Math.min(implicitWidth, bubbleColumn.width)")
              >= 2)
    check("qml pin: message time rendered from event ts",
          "ts: Number(ev.ts) > 0 ? Number(ev.ts) : 0" in chat_src
          and "ts: Date.now() / 1000" in chat_src
          and "page.formatTime(model.ts)" in chat_src
          and "function formatTime(ts)" in chat_src)
    # 10. Field lesson (2026-08-29, the owner lost their three service keys):
    #     a scanned bundle pre-fills fields, but each page's save button
    #     was separate from Continue — tapping through the wizard ended it,
    #     cleared the bundle, and the keys were never persisted. Every
    #     wizard page that accepts bundle/typed values must auto-commit
    #     filled fields from its Continue button (probe + save, advance
    #     only on success) and warn on unsaved input. No page's wizard
    #     Continue may call wizardNext() unconditionally anymore.
    with open(os.path.join(ROOT, "qml", "pages", "KeysPage.qml")) as f:
        keys_src = f.read()
    check("qml pin: KeysPage Continue commits unsaved keys",
          "function commitAndContinue()" in keys_src
          and "onClicked: page.commitAndContinue()" in keys_src
          and 'i18n.tr("Verify, save & continue")' in keys_src
          and "function saveKey(done)" in keys_src
          and keys_src.count("if (done) done(false);") >= 3
          and "if (done) done(true);" in keys_src)
    check("qml pin: KeysPage cards flag unsaved input",
          "readonly property bool unsaved" in keys_src
          and 'i18n.tr("Not saved yet' in keys_src)
    with open(os.path.join(ROOT, "qml", "pages", "TelegramPage.qml")) as f:
        tg_src = f.read()
    check("qml pin: TelegramPage Continue commits an unsaved token",
          "function saveTelegram(done)" in tg_src
          and "page.saveTelegram(function(ok) { if (ok) page.app.wizardNext(); });"
              in tg_src
          and 'i18n.tr("Verify, save & continue")' in tg_src
          and 'i18n.tr("Bot token not saved yet' in tg_src)
    with open(os.path.join(ROOT, "qml", "pages", "IdentityPage.qml")) as f:
        id_src = f.read()
    check("qml pin: IdentityPage Continue commits name and email",
          "function commitAndContinue()" in id_src
          and "onClicked: page.commitAndContinue()" in id_src
          and "readonly property bool namePending" in id_src
          and "readonly property bool emailPending" in id_src
          and "function saveName(done)" in id_src
          and "function saveEmail(done)" in id_src
          and 'i18n.tr("Verify, save & continue")' in id_src)
    with open(os.path.join(ROOT, "qml", "pages", "ProviderPage.qml")) as f:
        prov_src = f.read()
    check("qml pin: ProviderPage Continue commits a lingering key",
          "function save(done)" in prov_src
          and "page.save(function(ok) { if (ok) page.app.wizardNext(); });"
              in prov_src
          and 'i18n.tr("Verify, save & continue")' in prov_src)
    for rel, src in (("KeysPage.qml", keys_src), ("TelegramPage.qml", tg_src),
                     ("IdentityPage.qml", id_src), ("ProviderPage.qml", prov_src)):
        check("qml pin: %s has no unconditional wizard Continue" % rel,
              "onClicked: page.app.wizardNext()" not in src)


def run_draft_logic_tests():
    """Build/merge contract of qml/ChatDraftLogic.js under node — the exact
    file ChatPage imports. Pins Codex round 3's duplication: an unsent
    message lives in BOTH composer and pendingSends; only entries whose
    composer was actually cleared may be merged back on restore."""
    logic = os.path.join(ROOT, "qml", "ChatDraftLogic.js")
    if not shutil.which("node"):
        print("SKIP draft-logic (node missing)")
        return
    driver = os.path.join(WORK, "draftlogic_driver.js")
    with open(driver, "w") as f:
        f.write(
            "const fs = require('fs');\n"
            "eval(fs.readFileSync(process.argv[2], 'utf8'));\n"
            "const out = {};\n"
            "// 1. payload: ACTUAL clearing (not wire state) maps to\n"
            "// composer_cleared — round 4 made clearing conditional\n"
            "out.payload = buildDraftPayload('typed', ['/a.jpg'], {\n"
            "  s1: {text: 'on the wire', attachments: ['/w.pdf'],\n"
            "       sent: true, cleared: true},\n"
            "  s2: {text: 'typed', attachments: [], sent: true,\n"
            "       cleared: false},\n"
            "});\n"
            "// 2. the Codex repro: uncleared entry duplicates nothing\n"
            "out.dup = mergeDraft({composer: 'draft once', attachments: [],\n"
            "  pending: [{text: 'draft once', attachments: [],\n"
            "             composer_cleared: false}]}, '', []);\n"
            "// 3. cleared entries DO merge, ahead of the composer copy\n"
            "out.cleared = mergeDraft({composer: 'typed later',\n"
            "  attachments: ['/a.jpg'],\n"
            "  pending: [{text: 'was sent', attachments: ['/w.pdf', '/a.jpg'],\n"
            "             composer_cleared: true}]}, '', []);\n"
            "// 4. current composer content survives a restore\n"
            "out.current = mergeDraft({composer: 'old draft', attachments: [],\n"
            "  pending: []}, 'already typing', ['/c.png']);\n"
            "// 5. round 4 crash window: an uncleared pending record restores\n"
            "// via the composer copy but must still flag hadPending so the\n"
            "// delivery-uncertainty warning appears\n"
            "out.window = mergeDraft({composer: 'sent text', attachments: [],\n"
            "  pending: [{text: 'sent text', attachments: [],\n"
            "             composer_cleared: false}]}, '', []);\n"
            "// 6. round 4: the composer clears only on an exact snapshot\n"
            "// match; attachment-only sends never clear text\n"
            "out.clears = [shouldClearComposerText('hello', 'hello'),\n"
            "  shouldClearComposerText('  hello \\n', 'hello'),\n"
            "  shouldClearComposerText('hello plus newer typing', 'hello'),\n"
            "  shouldClearComposerText('hello', ''),\n"
            "  shouldClearComposerText('', '')];\n"
            "// 7. round 5: an UNCLEARED entry's attachments still restore —\n"
            "// chips leave the composer at wire time even when the text\n"
            "// stays, so the entry is their only surviving copy\n"
            "out.chips = mergeDraft({composer: 'sent text plus more',\n"
            "  attachments: [],\n"
            "  pending: [{text: 'sent text', attachments: ['/w.pdf'],\n"
            "             composer_cleared: false}]}, '', []);\n"
            "// 8. ...and one still in the composer list doesn't duplicate\n"
            "out.chipsDedup = mergeDraft({composer: '',\n"
            "  attachments: ['/w.pdf'],\n"
            "  pending: [{text: '', attachments: ['/w.pdf'],\n"
            "             composer_cleared: false}]}, '', []);\n"
            "console.log(JSON.stringify(out));\n")
    r = subprocess.run(["node", driver, logic],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        check("draft-logic runs under node", False, r.stderr.strip()[:300])
        return
    out = json.loads(r.stdout)
    payload = out["payload"]
    entries = sorted(payload["pending"], key=lambda p: p["text"])
    check("draft-logic: payload maps actual clearing → composer_cleared",
          payload["composer"] == "typed"
          and payload["attachments"] == ["/a.jpg"]
          and entries == [
              {"text": "on the wire", "attachments": ["/w.pdf"],
               "composer_cleared": True},
              {"text": "typed", "attachments": [],
               "composer_cleared": False}], str(payload))
    check("draft-logic: uncleared entry restores ONCE (no duplication)",
          out["dup"]["text"] == "draft once"
          and out["dup"]["restoredCount"] == 0, str(out["dup"]))
    check("draft-logic: cleared entry merges ahead of the composer copy",
          out["cleared"]["text"] == "was sent\ntyped later"
          and out["cleared"]["attachments"] == ["/a.jpg", "/w.pdf"]
          and out["cleared"]["restoredCount"] == 1, str(out["cleared"]))
    check("draft-logic: live composer content survives the restore",
          out["current"]["text"] == "old draft\nalready typing"
          and out["current"]["attachments"] == ["/c.png"], str(out["current"]))
    check("draft-logic: crash window flags hadPending without restoring twice",
          out["window"]["text"] == "sent text"
          and out["window"]["restoredCount"] == 0
          and out["window"]["hadPending"] is True, str(out["window"]))
    check("draft-logic: empty pending reports hadPending false",
          out["current"]["hadPending"] is False, str(out["current"]))
    check("draft-logic: composer clears only on exact snapshot match",
          out["clears"] == [True, True, False, False, False],
          str(out["clears"]))
    check("draft-logic: uncleared entry's attachments still restore",
          out["chips"]["text"] == "sent text plus more"
          and out["chips"]["attachments"] == ["/w.pdf"]
          and out["chips"]["restoredCount"] == 0
          and out["chips"]["hadPending"] is True, str(out["chips"]))
    check("draft-logic: attachment still in the composer list won't duplicate",
          out["chipsDedup"]["attachments"] == ["/w.pdf"],
          str(out["chipsDedup"]))


def run_markdown_tests():
    """qml/MarkdownLogic.js under node — the exact file ChatPage imports to
    render assistant bubbles. Chat-oriented, NOT CommonMark: single
    newlines stay <br/>, snake_case never italicizes."""
    logic = os.path.join(ROOT, "qml", "MarkdownLogic.js")
    if not shutil.which("node"):
        print("SKIP markdown (node missing)")
        return
    driver = os.path.join(WORK, "markdown_driver.js")
    with open(driver, "w") as f:
        f.write(
            "const fs = require('fs');\n"
            "eval(fs.readFileSync(process.argv[2], 'utf8'));\n"
            "const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));\n"
            "console.log(JSON.stringify(cases.map(c => toRichText(c))));\n")
    cases = [
        "plain text stays plain",
        "**bold** and *italic* words",
        "a `save_draft()` call",
        "5*3*2 = 30 and file_names_with_underscores",
        "- first\n- second",
        "# Header line",
        "see [the site](https://example.com/x?a=1&b=2) now",
        "```bash\nls -la  # two  spaces\n```",
        "1 < 2 & 3 > 2",
        "line one\nline two",
        "`code with **not bold** inside`",
        "**0.4.6** is now in Documents",
    ]
    cases_path = os.path.join(WORK, "markdown_cases.json")
    with open(cases_path, "w") as f:
        json.dump(cases, f)
    r = subprocess.run(["node", driver, logic, cases_path],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        check("markdown runs under node", False, r.stderr.strip()[:300])
        return
    out = json.loads(r.stdout)
    check("markdown: plain text unchanged",
          out[0] == "plain text stays plain", out[0])
    check("markdown: bold and italic",
          out[1] == "<b>bold</b> and <i>italic</i> words", out[1])
    check("markdown: inline code is monospace and untouched",
          out[2] == "a <tt>save_draft()</tt> call", out[2])
    check("markdown: math asterisks and snake_case survive",
          out[3] == "5*3*2 = 30 and file_names_with_underscores", out[3])
    check("markdown: bullets become dots, newline becomes <br/>",
          out[4] == "• first<br/>• second", out[4])
    check("markdown: header renders bold",
          out[5] == "<b>Header line</b>", out[5])
    check("markdown: links become anchors with escaped href",
          out[6] == 'see <a href="https://example.com/x?a=1&amp;b=2">'
                    "the site</a> now", out[6])
    check("markdown: fenced block is monospace, markers dropped, "
          "every space hardened to no-break",
          out[7] == "<tt>%s</tt>"
                    % "ls -la  # two  spaces".replace(" ", " "), out[7])
    check("markdown: HTML metacharacters escape",
          out[8] == "1 &lt; 2 &amp; 3 &gt; 2", out[8])
    check("markdown: single newlines preserved (no CommonMark collapse)",
          out[9] == "line one<br/>line two", out[9])
    check("markdown: bold pass can't touch code span contents",
          out[10] == "<tt>code with **not bold** inside</tt>", out[10])
    check("markdown: the field-test repro renders",
          out[11] == "<b>0.4.6</b> is now in Documents", out[11])


def main():
    try:
        run_client_tests()
        run_voice_tests()
        run_draft_logic_tests()
        run_markdown_tests()
        run_qml_checks()
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    print("\n%d passed, %d failed" % (PASSED, FAILED))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
