"""Voice-note recorder for the chat page.

Ubuntu Touch's QML stack (Qt 5) has no audio-only recorder type, but the
image ships GStreamer and PulseAudio — so recording is a supervised
`gst-launch-1.0` child writing an Ogg/Opus file, with `arecord` (WAV) as a
fallback. `-e` + SIGINT makes GStreamer emit EOS and finalize the container,
so a stopped recording is a valid file, not a truncated stream.

The recorded file is handed to Ada over the chat socket (`voice` request);
Ada transcribes it and the app deletes the file after the ack/nack — the
recording never needs to outlive the exchange.

Duration is capped in three layers, because the QML timer alone is not
enough: Lomiri can suspend the app's process while recording, freezing both
the QML timer AND any Python thread — but NOT the recorder child, which
would then run for hours. So:
  1. the recorder child is wrapped in coreutils `timeout -s INT` (its own
     process, immune to app suspension) — the layer that actually holds when
     the UI is frozen;
  2. a Python watchdog thread finalizes a recording the UI forgot to stop
     while the app is alive (page crash, lost signal);
  3. the QML timer remains the cooperative, visible stop.
Stale files from a killed app (pending recordings are otherwise deleted only
after ack/nack) are swept at the next start_recording().
"""

import os
import shutil
import signal
import subprocess
import threading
import time

# Env override is a test seam (selftests must never write into the real
# cache); the app itself always uses the default.
RECORD_DIR = os.environ.get(
    "ADA_UT_VOICE_DIR",
    os.path.expanduser("~/.cache/ada.permaevidence/voice"))
# A phone voice note, not a podcast: the UI also shows elapsed time.
# Module attribute (not a constant baked into closures) so tests can shrink
# it to exercise the watchdog layers in seconds.
MAX_SECONDS = 300
# The timeout(1) wrapper fires this long AFTER the cap, so the cooperative
# layers always win when the app is alive and the file finalizes exactly once.
HARD_CAP_GRACE = 10
# Recordings a killed/backgrounded app never settled are garbage after this.
STALE_AFTER_SECONDS = 2 * 3600

_current = {"proc": None, "path": None, "backend": None, "started": 0.0}


def backend_info():
    """Which recorder this device can use, if any."""
    if shutil.which("gst-launch-1.0"):
        return {"ok": True, "backend": "gstreamer"}
    if shutil.which("arecord"):
        return {"ok": True, "backend": "arecord"}
    return {"ok": False,
            "error": "no audio recorder found on this device "
                     "(needs gst-launch-1.0 or arecord)"}


def _build_argv(backend, path):
    if backend == "gstreamer":
        return ["gst-launch-1.0", "-q", "-e",
                "pulsesrc",
                "!", "audioconvert",
                "!", "audioresample",
                "!", "opusenc", "bitrate=24000",
                "!", "oggmux",
                "!", "filesink", "location=%s" % path]
    return ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", path]


def _full_argv(backend, path):
    """Recorder argv, wrapped in the suspension-proof hard cap when the
    device has coreutils timeout (Ubuntu Touch always does; a dev Mac may
    not — there the Python watchdog is the outer layer instead). SIGINT so
    the recorder finalizes its container; -k escalates if it won't die."""
    argv = _build_argv(backend, path)
    if shutil.which("timeout"):
        argv = ["timeout", "-s", "INT", "-k", "10",
                str(MAX_SECONDS + HARD_CAP_GRACE)] + argv
    return argv


def _sweep_stale_files():
    """Delete finished-but-never-settled recordings (killed app, page closed
    with a voice request in flight). The active recording is exempt."""
    active = _current["path"]
    try:
        names = os.listdir(RECORD_DIR)
    except OSError:
        return
    cutoff = time.time() - STALE_AFTER_SECONDS
    for name in names:
        full = os.path.join(RECORD_DIR, name)
        if full == active:
            continue
        try:
            if os.path.isfile(full) and os.path.getmtime(full) < cutoff:
                os.remove(full)
        except OSError:
            pass


def _watchdog(proc, deadline):
    """Layer 2: finalize a recording the UI never stopped. Runs while the
    app is alive; when the whole process is suspended, layer 1 (timeout)
    covers instead. Only finalizes — state cleanup still happens through
    stop/cancel so the UI's view of the world stays consistent."""
    while time.time() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.5)
    if _current["proc"] is proc:
        _finalize(proc)


def start_recording():
    if _current["proc"] is not None:
        return {"ok": False, "error": "already recording"}
    info = backend_info()
    if not info["ok"]:
        return info
    try:
        os.makedirs(RECORD_DIR, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    _sweep_stale_files()
    extension = "ogg" if info["backend"] == "gstreamer" else "wav"
    path = os.path.join(
        RECORD_DIR, "voice-%s.%s" % (time.strftime("%Y%m%d-%H%M%S"), extension))
    try:
        proc = subprocess.Popen(
            _full_argv(info["backend"], path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE)
    except OSError as exc:
        return {"ok": False, "error": "could not start recorder: %s" % exc}
    # An immediately-dead child (no mic permission, missing plugin) must be
    # a readable error, not a mystery empty file at stop time.
    time.sleep(0.4)
    if proc.poll() is not None:
        stderr = (proc.stderr.read() or b"").decode("utf-8", "replace")
        _cleanup_file(path)
        return {"ok": False,
                "error": "recorder exited immediately: %s"
                         % (stderr.strip()[-400:] or "no error output")}
    started = time.time()
    _current.update(proc=proc, path=path,
                    backend=info["backend"], started=started)
    threading.Thread(
        target=_watchdog, args=(proc, started + MAX_SECONDS + 2),
        daemon=True).start()
    return {"ok": True, "path": path, "backend": info["backend"],
            "max_seconds": MAX_SECONDS}


def stop_recording():
    """Finalize and return the file for sending."""
    proc, path = _current["proc"], _current["path"]
    started = _current["started"]
    if proc is None:
        return {"ok": False, "error": "not recording"}
    _current.update(proc=None, path=None, backend=None, started=0.0)
    _finalize(proc)
    # Settle window: some recorders flush the container a beat after exiting
    # (and a cap-expired child may still be mid-finalize when the user taps
    # stop) — poll briefly instead of judging the first stat.
    size = 0
    deadline = time.time() + 2.0
    while True:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if size > 128 or time.time() >= deadline:
            break
        time.sleep(0.1)
    if size <= 128:  # container header only — nothing was captured
        _cleanup_file(path)
        return {"ok": False,
                "error": "the recording is empty — check the microphone"}
    return {"ok": True, "path": path, "size": size,
            "seconds": round(time.time() - started, 1)}


def cancel_recording():
    proc, path = _current["proc"], _current["path"]
    if proc is None:
        return {"ok": True, "was_recording": False}
    _current.update(proc=None, path=None, backend=None, started=0.0)
    _finalize(proc)
    _cleanup_file(path)
    return {"ok": True, "was_recording": True}


def elapsed_seconds():
    if _current["proc"] is None:
        return 0
    return int(time.time() - _current["started"])


def delete_recording(path):
    """Remove a finished note after Ada acked/nacked it. Only files inside
    our own recording directory are deletable — this is not a general rm."""
    real = os.path.realpath(path or "")
    if not real.startswith(os.path.realpath(RECORD_DIR) + os.sep):
        return {"ok": False, "error": "not a recording file"}
    _cleanup_file(real)
    return {"ok": True}


def _finalize(proc):
    # SIGINT: gst-launch -e turns it into EOS + clean finalize; arecord
    # closes the WAV header; the timeout wrapper forwards it to its child.
    # SIGKILL only as the escalation of last resort. Safe to call on an
    # already-exited child (the cap fired first): signals to a reaped-by-us
    # process just error out and the waits return immediately.
    try:
        proc.send_signal(signal.SIGINT)
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _cleanup_file(path):
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass
