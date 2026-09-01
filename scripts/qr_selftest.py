#!/usr/bin/env python3
"""Offline selftest for the QR key-transfer stack (py/qr_scan.py decoder,
scripts/qr_ref.py reference encoder, and — when node plus the website's
qrlib.mjs are reachable — the JS generator port).

Deterministic and hermetic: seeded randomness, all files under a private
temp dir, no network. Run:  python3 scripts/qr_selftest.py
"""

import json
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "py"))

import qr_ref
import qr_scan

# The website's JS generator port, cross-checked matrix-for-matrix below.
# Resolution: explicit override → the website checkout next to this repo
# (drift check against the live page source) → the copy vendored under
# scripts/fixtures/ (what CI runs, so the cross-check never silently skips).
_WEBSITE_QRLIB = os.path.expanduser("~/Desktop/briglia-website/app/qr/qrlib.mjs")
_VENDORED_QRLIB = os.path.join(HERE, "fixtures", "qrlib.mjs")
JS_LIB = os.environ.get("BRIGLIA_QRLIB_JS") or (
    _WEBSITE_QRLIB if os.path.isfile(_WEBSITE_QRLIB) else _VENDORED_QRLIB)

CHECKS = [0, 0]  # passed, failed


def check(name, condition, detail=""):
    if condition:
        CHECKS[0] += 1
        print("ok   %s" % name)
    else:
        CHECKS[1] += 1
        print("FAIL %s%s" % (name, " — " + str(detail) if detail else ""))


ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:/._-{}\""


def random_text(rng, n):
    return "".join(rng.choice(ALPHABET) for _ in range(n))


def test_tables():
    totals = {1: 26, 2: 44, 3: 70, 4: 100, 5: 134,
              6: 172, 7: 196, 8: 242, 9: 292, 10: 346}
    for version, expected in totals.items():
        for level in "LMQH":
            got = sum(d + e for d, e in qr_scan.EC_BLOCKS[version][level])
            check("ec-table v%d-%s total" % (version, level), got == expected,
                  "%d != %d" % (got, expected))


def test_clean_roundtrip(tmp):
    rng = random.Random(7)
    path = os.path.join(tmp, "clean.png")
    bad = []
    for version in range(1, 11):
        for level in "LMQH":
            cap = qr_ref._capacity(version, level)
            text = random_text(rng, max(1, cap - 1))
            qr_ref.encode_to_png(text, path, level=level)
            if qr_scan.decode_png(path) != text:
                bad.append("v%d-%s" % (version, level))
    check("clean round-trip 40 combos", not bad, bad)


def _warped_case(rng, tmp, version, level, corners, scale=6, noise=8):
    cap = qr_ref._capacity(version, level)
    text = random_text(rng, max(1, cap - 1))
    matrix, _ = qr_ref.encode(text, level, version=version)
    pixels, size = qr_ref.render_gray(matrix, scale, 4, dark=40, light=200)
    out = qr_ref.warp_gray(pixels, size, 640, 480, corners,
                           background=170, noise=noise)
    path = os.path.join(tmp, "warp.png")
    qr_ref.write_png_gray(path, out, 640, 480)
    return text, qr_scan.decode_png(path)


def test_warped(tmp):
    strong = [(90.0, 60.0), (560.0, 85.0), (545.0, 430.0), (105.0, 405.0)]
    mild = [(100.0, 50.0), (540.0, 58.0), (536.0, 428.0), (104.0, 420.0)]
    rng = random.Random(11)
    bad = []
    # v1 has no alignment pattern: only the mild case is guaranteed.
    for version in (2, 4, 6, 7, 8, 10):
        for level in ("L", "M"):
            text, got = _warped_case(rng, tmp, version, level, strong)
            if got != text:
                bad.append("strong v%d-%s" % (version, level))
    check("strong perspective (v2-10)", not bad, bad)
    bad = []
    for version in (1, 2, 6, 8, 10):
        for level in ("L", "M"):
            text, got = _warped_case(rng, tmp, version, level, mild)
            if got != text:
                bad.append("mild v%d-%s" % (version, level))
    check("mild perspective (v1-10)", not bad, bad)


def test_random_sweep(tmp):
    rng = random.Random(42)
    path = os.path.join(tmp, "sweep.png")
    failures = 0
    trials = 30
    for _ in range(trials):
        version = rng.choice([3, 4, 5, 6, 7, 8])
        level = rng.choice(["L", "M"])
        cap = qr_ref._capacity(version, level)
        text = random_text(rng, rng.randint(max(1, cap // 2), cap - 1))
        matrix, _ = qr_ref.encode(text, level, version=version)
        scale = rng.choice([4, 5, 6, 7])
        pixels, size = qr_ref.render_gray(
            matrix, scale, 4, dark=rng.randint(10, 60),
            light=rng.randint(180, 240))
        m = 60
        corners = [(m + rng.uniform(-35, 35), m + rng.uniform(-35, 35)),
                   (640 - m + rng.uniform(-35, 35), m + rng.uniform(-35, 35)),
                   (640 - m + rng.uniform(-35, 35), 480 - m + rng.uniform(-35, 35)),
                   (m + rng.uniform(-35, 35), 480 - m + rng.uniform(-35, 35))]
        out = qr_ref.warp_gray(pixels, size, 640, 480, corners,
                               background=rng.randint(120, 200),
                               noise=rng.randint(0, 12))
        qr_ref.write_png_gray(path, out, 640, 480)
        if qr_scan.decode_png(path) != text:
            failures += 1
    # The scanner sees many frames per code in practice; require a high
    # single-frame rate rather than perfection under this deliberately
    # harsh distribution.
    check("random harsh sweep >= %d/%d" % (trials - 2, trials),
          failures <= 2, "%d failures" % failures)


def test_png_variants(tmp):
    # 1-bit grayscale PNG (what the python-qrcode library emits).
    matrix, _ = qr_ref.encode("one-bit-png-test", "M")
    dim = len(matrix)
    scale, quiet = 6, 4
    size = (dim + 2 * quiet) * scale
    import struct
    stride = (size + 7) // 8
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        rowbits = bytearray(stride)
        for x in range(size):
            mr, mc = y // scale - quiet, x // scale - quiet
            dark = 0 <= mr < dim and 0 <= mc < dim and matrix[mr][mc]
            if not dark:
                rowbits[x >> 3] |= 1 << (7 - (x & 7))
        raw.extend(rowbits)

    def chunk(ctype, data):
        body = ctype + data
        return struct.pack(">I", len(data)) + body \
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    path = os.path.join(tmp, "onebit.png")
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 1, 0, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw))))
        f.write(chunk(b"IEND", b""))
    check("1-bit PNG decode", qr_scan.decode_png(path) == "one-bit-png-test")


def test_framing():
    payload = json.dumps({"v": 1, "keys": {"serper": "s" * 40}},
                         separators=(",", ":"))
    frames = qr_scan.make_frames(payload, chunk_len=30)
    check("make_frames splits", len(frames) > 1)
    parsed = qr_scan.parse_frame(frames[0])
    check("parse_frame round-trip", parsed is not None and parsed[0] == 1
          and parsed[1] == len(frames))
    reassembled = "".join(qr_scan.parse_frame(f)[3] for f in frames)
    check("chunks reassemble", reassembled == payload)
    crc = "%08x" % (zlib.crc32(payload.encode()) & 0xFFFFFFFF)
    check("crc in frame", qr_scan.parse_frame(frames[0])[2] == crc)
    for bad in ("ADAK1:0/2:%s:x" % crc, "ADAK1:3/2:%s:x" % crc,
                "ADAK1:1/99:%s:x" % crc, "ADAK1:1/2:zzzz:x",
                "ADAK1:12:%s:x" % crc, "not-a-frame", "ADAK2:1/1:%s:x" % crc):
        check("parse_frame rejects %r" % bad[:18],
              qr_scan.parse_frame(bad) is None)

    # session assembly: out of order, duplicates, mismatch, corrupt
    qr_scan.reset_session()
    r = qr_scan._feed_bundle(frames[1])
    check("session partial", r["kind"] == "bundle" and r["done"] is False
          and r["have"] == 1)
    r = qr_scan._feed_bundle(frames[1])  # duplicate: no double-count
    check("session duplicate", r["have"] == 1 if r.get("done") is not True
          else False)
    other = qr_scan.make_frames('{"v":1,"keys":{"jina":"other"}}')[0]
    r = qr_scan._feed_bundle(other)
    check("session mismatch refused", r["kind"] == "bundle_mismatch")
    for f in frames:
        r = qr_scan._feed_bundle(f)
    check("session completes", r.get("done") is True
          and r["keys"] == {"serper": "s" * 40})
    # corrupt: right frame shape, wrong crc for the assembled payload
    qr_scan.reset_session()
    forged = "ADAK1:1/1:%s:%s" % ("0" * 8, payload)
    r = qr_scan._feed_bundle(forged)
    check("checksum failure restarts", r["kind"] == "bundle_corrupt")

    # parse_bundle guards
    keys, ignored = qr_scan.parse_bundle(
        '{"v":1,"keys":{"openai":"sk-x","mystery":"y","jina":"  "}}')
    check("bundle known keys", keys == {"openai": "sk-x"})
    check("bundle unknown ignored", ignored == ["mystery"])
    for bad_payload in ('[]', '{"v":2,"keys":{}}', '{"v":1,"keys":{"openai":1}}',
                        '{"v":1,"keys":{}}', 'not json'):
        try:
            qr_scan.parse_bundle(bad_payload)
            check("parse_bundle rejects %r" % bad_payload[:20], False)
        except ValueError:
            check("parse_bundle rejects %r" % bad_payload[:20], True)


def test_scan_png_modes(tmp):
    path = os.path.join(tmp, "single.png")
    qr_ref.encode_to_png("sk-plain-key-123", path)
    r = qr_scan.scan_png(path, "single")
    check("single mode text", r.get("kind") == "text"
          and r.get("text") == "sk-plain-key-123")
    check("frame file deleted", not os.path.exists(path))

    frame = qr_scan.make_frames('{"v":1,"keys":{"jina":"j"}}')[0]
    qr_ref.encode_to_png(frame, path)
    r = qr_scan.scan_png(path, "single")
    check("bundle-in-single hint", r.get("kind") == "bundle_in_single")

    qr_scan.reset_session()
    qr_ref.encode_to_png(frame, path)
    r = qr_scan.scan_png(path, "bundle")
    check("bundle mode completes", r.get("done") is True
          and r.get("keys") == {"jina": "j"})

    qr_ref.encode_to_png("just some text", path)
    qr_scan.reset_session()
    r = qr_scan.scan_png(path, "bundle")
    check("non-bundle in bundle mode", r.get("kind") == "not_bundle")

    r = qr_scan.scan_png(os.path.join(tmp, "missing.png"), "single")
    check("missing file handled", r.get("found") is False and "error" in r)


def test_frame_paths(tmp):
    import time
    frame_dir = os.path.join(tmp, "frames")
    os.environ["BRIGLIA_QR_FRAME_DIR"] = frame_dir
    try:
        p1 = qr_scan.frame_path(1)
        p2 = qr_scan.frame_path(2)
        check("frame paths unique per generation (BMP fast path)",
              p1 != p2 and p1.endswith("qr-frame-1.bmp")
              and os.path.dirname(p1) == frame_dir)
        stale_bmp = os.path.join(frame_dir, "qr-frame-901.bmp")
        with open(stale_bmp, "w") as f:
            f.write("x")
        very_old = __import__("time").time() - 300
        os.utime(stale_bmp, (very_old, very_old))
        qr_scan.frame_path(2)
        check("stale BMP frames purged", not os.path.exists(stale_bmp))
        stale = os.path.join(frame_dir, "qr-frame-999.png")
        with open(stale, "w") as f:
            f.write("x")
        old = time.time() - 300
        os.utime(stale, (old, old))
        fresh = os.path.join(frame_dir, "qr-frame-998.png")
        with open(fresh, "w") as f:
            f.write("x")
        qr_scan.frame_path(3)
        check("stale frames purged", not os.path.exists(stale))
        check("fresh frames kept", os.path.exists(fresh))

        # photo-mode paths share the dir and the purge sweep
        j1 = qr_scan.photo_path(1)
        check("photo paths unique per generation",
              j1.endswith("qr-photo-1.jpg")
              and os.path.dirname(j1) == frame_dir)
        stale_jpg = os.path.join(frame_dir, "qr-photo-77.jpg")
        with open(stale_jpg, "w") as f:
            f.write("x")
        os.utime(stale_jpg, (old, old))
        qr_scan.frame_path(4)
        check("stale photos purged", not os.path.exists(stale_jpg))

        # remove_file: only qr-* names inside the scanner cache dir
        victim = os.path.join(frame_dir, "qr-photo-5.jpg")
        with open(victim, "w") as f:
            f.write("x")
        check("remove_file deletes scanner temp",
              qr_scan.remove_file(victim) and not os.path.exists(victim))
        outside = os.path.join(tmp, "qr-photo-6.jpg")
        with open(outside, "w") as f:
            f.write("x")
        check("remove_file refuses outside dir",
              qr_scan.remove_file(outside) is False and os.path.exists(outside))
        inside_other = os.path.join(frame_dir, "keep.txt")
        with open(inside_other, "w") as f:
            f.write("x")
        check("remove_file refuses non-qr names",
              qr_scan.remove_file(inside_other) is False
              and os.path.exists(inside_other))
    finally:
        del os.environ["BRIGLIA_QR_FRAME_DIR"]


def test_blank_frames(tmp):
    """The photo-mode trigger: uniform frames (grabToImage of an external
    GPU texture) must be reported as blank; textured-but-undecodable and
    decodable frames must not."""
    path = os.path.join(tmp, "blank.png")
    qr_ref.write_png_gray(path, bytearray([128] * (200 * 150)), 200, 150)
    r = qr_scan.scan_png(path, "single")
    check("uniform frame reported blank",
          r.get("found") is False and r.get("blank") is True, r)

    # near-uniform (below the spread threshold) still counts as blank
    pixels = bytearray([120 + (i % 4) for i in range(200 * 150)])
    qr_ref.write_png_gray(path, pixels, 200, 150)
    r = qr_scan.scan_png(path, "single")
    check("near-uniform frame reported blank", r.get("blank") is True, r)

    # real-world texture without a QR: not blank
    pixels = bytearray([(x * 7 + y * 13) % 256
                        for y in range(150) for x in range(200)])
    qr_ref.write_png_gray(path, pixels, 200, 150)
    r = qr_scan.scan_png(path, "single")
    check("textured frame not blank",
          r.get("found") is False and r.get("blank") is not True, r)

    # decodable frames are unaffected by the blank check
    qr_ref.encode_to_png("still-decodes", path)
    r = qr_scan.scan_png(path, "single")
    check("decodable frame unaffected by blank check",
          r.get("kind") == "text" and r.get("text") == "still-decodes", r)


def write_bmp(path, gray, w, h, bpp=32, bottom_up=True, bitfields=False):
    """Qt-style BMP writer for loader tests (BGRA/BGR, padded strides)."""
    px = bpp // 8
    stride = ((bpp * w + 31) // 32) * 4
    rows = []
    for y in range(h):
        row = bytearray()
        for x in range(w):
            v = gray[y * w + x]
            row += bytes((v, v, v, 255)) if px == 4 else bytes((v, v, v))
        row += b"\x00" * (stride - len(row))
        rows.append(bytes(row))
    height_field = h if bottom_up else -h
    if bottom_up:
        rows = rows[::-1]
    extra = struct.pack("<III", 0xFF0000, 0xFF00, 0xFF) if bitfields else b""
    pixel_off = 14 + 40 + len(extra)
    data = b"".join(rows)
    header = b"BM" + struct.pack("<IHHI", pixel_off + len(data), 0, 0,
                                 pixel_off)
    dib = struct.pack("<IiiHHIIiiII", 40, w, height_field, 1, bpp,
                      3 if bitfields else 0, len(data), 2835, 2835, 0, 0)
    with open(path, "wb") as f:
        f.write(header + dib + extra + data)


def test_bmp_frames(tmp):
    """The on-device fast path: camera grabs are saved as BMP (no zlib,
    no per-byte defiltering — that cost ~5s/frame on a Pixel 3a). The
    bottom-up case doubles as the flip test: a vertically mirrored QR
    cannot decode, so success proves the un-flip."""
    text = "bmp-fast-path-key"
    matrix, _ = qr_ref.encode(text, level="M")
    pixels, size = qr_ref.render_gray(matrix, scale=6, quiet=4)
    # non-square canvas with an odd width so 24bpp rows need padding
    W, H = size + 27, size + 10
    canvas = bytearray([225]) * (W * H)
    for y in range(size):
        canvas[(y + 5) * W + 9:(y + 5) * W + 9 + size] = \
            pixels[y * size:(y + 1) * size]
    p = os.path.join(tmp, "frame.bmp")
    for name, kwargs in (
            ("32bpp bottom-up", dict(bpp=32, bottom_up=True)),
            ("24bpp top-down padded", dict(bpp=24, bottom_up=False)),
            ("24bpp bottom-up padded", dict(bpp=24, bottom_up=True)),
            ("32bpp BI_BITFIELDS", dict(bpp=32, bitfields=True))):
        write_bmp(p, canvas, W, H, **kwargs)
        r = qr_scan.scan_png(p, "single")
        check("BMP %s decodes" % name,
              r.get("found") is True and r.get("text") == text, r)
    write_bmp(p, canvas, W, H, bpp=32)
    with open(p, "r+b") as f:
        f.seek(30)
        f.write(struct.pack("<I", 1))  # BI_RLE8: unsupported compression
    r = qr_scan.scan_png(p, "single")
    check("BMP unsupported compression → readable error",
          r.get("found") is False and "compression" in str(r.get("error")), r)
    check("env_info shape", qr_scan.env_info().startswith("py3")
          and "numpy" in qr_scan.env_info(), qr_scan.env_info())


def test_qml_callback_contract(tmp):
    """A JS function cannot survive Lomiri PageStack.push properties
    (QVariantMap conversion drops it silently) — the scan callback must
    be registered on the app root instead. This exact silence cost a
    field round on 2026-08-28."""
    base = os.path.dirname(HERE)
    with open(os.path.join(base, "qml", "Main.qml")) as f:
        main_src = f.read()
    with open(os.path.join(base, "qml", "pages", "ScanPage.qml")) as f:
        scan_src = f.read()
    check("openScan registers the callback on the root, not in push props",
          "scanCallback = cb" in main_src and "callback: cb" not in main_src)
    check("ScanPage delivers via app.scanCallback",
          "app.scanCallback" in scan_src)
    check("ScanPage logs a missing callback loudly",
          "NO delivery callback" in scan_src)


def test_debug_diagnostics(tmp):
    """Opt-in field diagnostics: frame copies rotate in the debug dir,
    scan-log lines are appended, results carry ms/cands/dim, and
    finish_photo keeps the same cache-dir/qr-* discipline as remove_file."""
    debug_dir = os.path.join(tmp, "qr-debug")
    frame_dir = os.path.join(tmp, "frames2")
    os.environ["BRIGLIA_QR_DEBUG_DIR"] = debug_dir
    os.environ["BRIGLIA_QR_FRAME_DIR"] = frame_dir
    try:
        path = os.path.join(tmp, "dbg.png")

        # debug OFF: nothing persisted, no diagnostics fields promised
        qr_ref.encode_to_png("first-code", path)
        r = qr_scan.scan_png(path, "single")
        check("debug off leaves no debug dir contents",
              not os.path.exists(os.path.join(debug_dir, "last-frame.png"))
              and not os.path.exists(os.path.join(debug_dir, "scan-log.txt")))

        # debug ON: decodable frame → copy + log line + diagnostics fields
        qr_ref.encode_to_png("first-code", path)
        r = qr_scan.scan_png(path, "single", True)
        last = os.path.join(debug_dir, "last-frame.png")
        log = os.path.join(debug_dir, "scan-log.txt")
        check("debug scan decodes with diagnostics",
              r.get("kind") == "text" and isinstance(r.get("ms"), int)
              and isinstance(r.get("cands"), int) and r.get("cands") >= 3
              and "x" in str(r.get("dim")), r)
        check("debug frame copy saved", os.path.exists(last))
        check("original frame still deleted", not os.path.exists(path))
        with open(log) as f:
            text = f.read()
        check("decode logged", "DECODED" in text, text)

        # second scan rotates last → prev
        first_bytes = open(last, "rb").read()
        qr_ref.encode_to_png("second-code", path)
        r = qr_scan.scan_png(path, "single", True)
        prev = os.path.join(debug_dir, "prev-frame.png")
        check("frame rotation keeps previous",
              os.path.exists(prev) and open(prev, "rb").read() == first_bytes)
        check("last frame replaced",
              open(last, "rb").read() != first_bytes)

        # undecodable frame logs candidate counts
        pixels = bytearray([(x * 7 + y * 13) % 256
                            for y in range(150) for x in range(200)])
        qr_ref.write_png_gray(path, pixels, 200, 150)
        r = qr_scan.scan_png(path, "single", True)
        check("no-decode carries diagnostics",
              r.get("found") is False and isinstance(r.get("cands"), int)
              and isinstance(r.get("ms"), int), r)
        with open(log) as f:
            text = f.read()
        check("no-decode logged with candidates",
              "no decode" in text and "finder candidates" in text, text)

        # blank frame logged as such
        qr_ref.write_png_gray(path, bytearray([128] * (200 * 150)), 200, 150)
        r = qr_scan.scan_png(path, "single", True)
        check("blank carries diagnostics", r.get("blank") is True
              and isinstance(r.get("ms"), int), r)

        # log_event: gated on the debug flag
        check("log_event off is a no-op",
              qr_scan.log_event("hidden", False) is False)
        check("log_event on writes",
              qr_scan.log_event("camera says hi", True) is True
              and "camera says hi" in open(log).read())

        # log cap: oversized log is reset rather than growing forever
        with open(log, "w") as f:
            f.write("x" * 250_000)
        qr_scan.log_event("fresh line", True)
        check("oversized log reset", os.path.getsize(log) < 1000)

        # finish_photo: debug keeps a rotated copy, then deletes; refuses
        # paths outside the scanner cache dir exactly like remove_file
        jpg = qr_scan.photo_path(9)
        with open(jpg, "wb") as f:
            f.write(b"jpegbytes-1")
        check("finish_photo debug keeps copy",
              qr_scan.finish_photo(jpg, True) and not os.path.exists(jpg)
              and open(os.path.join(debug_dir, "last-photo.jpg"), "rb").read()
              == b"jpegbytes-1")
        jpg2 = qr_scan.photo_path(10)
        with open(jpg2, "wb") as f:
            f.write(b"jpegbytes-2")
        check("finish_photo rotates photos",
              qr_scan.finish_photo(jpg2, True)
              and open(os.path.join(debug_dir, "prev-photo.jpg"), "rb").read()
              == b"jpegbytes-1"
              and open(os.path.join(debug_dir, "last-photo.jpg"), "rb").read()
              == b"jpegbytes-2")
        jpg3 = qr_scan.photo_path(11)
        with open(jpg3, "wb") as f:
            f.write(b"jpegbytes-3")
        check("finish_photo non-debug just deletes",
              qr_scan.finish_photo(jpg3, False) and not os.path.exists(jpg3)
              and open(os.path.join(debug_dir, "last-photo.jpg"), "rb").read()
              == b"jpegbytes-2")
        outside = os.path.join(tmp, "qr-photo-evil.jpg")
        with open(outside, "w") as f:
            f.write("x")
        check("finish_photo refuses outside dir",
              qr_scan.finish_photo(outside, True) is False
              and os.path.exists(outside))
    finally:
        del os.environ["BRIGLIA_QR_DEBUG_DIR"]
        del os.environ["BRIGLIA_QR_FRAME_DIR"]


def test_scanned_key_logic(tmp):
    """Exact-value bundle-ownership contract (qml/ScannedKeyLogic.js),
    exercised under node — the same file the QML pages import. Pins the
    two Codex round-3 scenarios: bundle replacement must update a still-
    owned field, and discard must NOT erase a hand-edited value."""
    logic = os.path.join(os.path.dirname(HERE), "qml", "ScannedKeyLogic.js")
    if not shutil.which("node"):
        print("skip scanned-key-logic (node missing)")
        return
    cases = [
        # (fieldText, injected, scanned) -> (text, injected)
        ("", "", "A", "A", "A"),          # inject into empty field
        ("A", "A", "B", "B", "B"),        # bundle replacement, still owned
        ("A", "A", "", "", ""),           # discard/consume clears owned
        ("A2", "A", "", "A2", ""),        # discard preserves edited value
        ("A2", "A", "B", "A2", "A"),      # replacement never touches edits
        ("T", "", "A", "T", ""),          # typed-before-scan is user-owned
        ("", "", "", "", ""),             # nothing anywhere
        ("", "A", "A", "A", "A"),         # emptied field re-accepts entry
        ("", "A", "", "", ""),            # save-cleared, entry gone
    ]
    driver = os.path.join(tmp, "scanlogic_driver.js")
    with open(driver, "w") as f:
        f.write(
            "const fs = require('fs');\n"
            "eval(fs.readFileSync(process.argv[2], 'utf8'));\n"
            "const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));\n"
            "console.log(JSON.stringify(cases.map(c =>\n"
            "  sync(c[0], c[1], c[2] === '' ? undefined : c[2]))));\n")
    cases_path = os.path.join(tmp, "scanlogic_cases.json")
    with open(cases_path, "w") as f:
        json.dump([[c[0], c[1], c[2]] for c in cases], f)
    r = subprocess.run(["node", driver, logic, cases_path],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        check("scanned-key-logic runs", False, r.stderr.strip()[:200])
        return
    results = json.loads(r.stdout)
    for case, result in zip(cases, results):
        want = {"text": case[3], "injected": case[4]}
        check("scan-logic %r/%r/%r" % (case[0], case[1], case[2]),
              result == want, "%s != %s" % (result, want))


def _node_matrix(text, level):
    r = subprocess.run(["node", JS_LIB, "matrix", level, text],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:200])
    lines = r.stdout.strip().split("\n")
    return int(lines[0]), [[int(ch) for ch in row] for row in lines[1:]]


def test_js_port(tmp):
    if os.path.abspath(JS_LIB) != os.path.abspath(_VENDORED_QRLIB) and os.path.isfile(JS_LIB):
        with open(JS_LIB, "rb") as f_live, open(_VENDORED_QRLIB, "rb") as f_vendored:
            check("vendored scripts/fixtures/qrlib.mjs == the website's generator (no drift)",
                  f_live.read() == f_vendored.read(), JS_LIB)
    if not (shutil.which("node") and os.path.isfile(JS_LIB)):
        print("skip js-port cross-check (node or %s missing)" % JS_LIB)
        return
    rng = random.Random(33)
    bad = []
    for version in (1, 3, 5, 7, 8, 10):
        for level in "LM":
            cap = qr_ref._capacity(version, level)
            text = random_text(rng, max(1, cap - 1))
            py_matrix, _ = qr_ref.encode(text, level, version=version)
            js_version, js_matrix = _node_matrix(text, level)
            if js_version != version or js_matrix != py_matrix:
                bad.append("v%d-%s" % (version, level))
    check("js matrices byte-identical (12 combos)", not bad, bad)

    payload = json.dumps(
        {"v": 1, "keys": {"openai": "sk-" + "x" * 90,
                          "telegram_token": "12345:AAtest"}},
        separators=(",", ":"))
    r = subprocess.run(["node", JS_LIB, "frames", "_", payload],
                       capture_output=True, text=True, timeout=30)
    js_frames = r.stdout.strip().split("\n")
    check("js frames identical", js_frames == qr_scan.make_frames(payload))

    # end-to-end: JS-encoded bundle frames, warped, through the scanner
    keys = {"opencode": "sk-oc-" + "a1b2" * 12, "serper": "f" * 40,
            "telegram_chat_id": "5551234567"}
    payload = json.dumps({"v": 1, "keys": keys}, separators=(",", ":"))
    r = subprocess.run(["node", JS_LIB, "frames", "_", payload],
                       capture_output=True, text=True, timeout=30)
    frames = r.stdout.strip().split("\n")
    qr_scan.reset_session()
    mild = [(100.0, 50.0), (540.0, 58.0), (536.0, 428.0), (104.0, 420.0)]
    result = None
    path = os.path.join(tmp, "e2e.png")
    for frame in reversed(frames):  # out of order on purpose
        _, matrix = _node_matrix(frame, "M")
        pixels, size = qr_ref.render_gray(matrix, 6, 4, dark=35, light=210)
        out = qr_ref.warp_gray(pixels, size, 640, 480, mild,
                               background=165, noise=6)
        qr_ref.write_png_gray(path, out, 640, 480)
        result = qr_scan.scan_png(path, "bundle")
    check("js->python end-to-end bundle",
          result is not None and result.get("done") is True
          and result.get("keys") == keys,
          result)


def main():
    tmp = tempfile.mkdtemp(prefix="briglia-qr-selftest-")
    try:
        test_tables()
        test_clean_roundtrip(tmp)
        test_warped(tmp)
        test_random_sweep(tmp)
        test_png_variants(tmp)
        test_framing()
        test_scan_png_modes(tmp)
        test_frame_paths(tmp)
        test_blank_frames(tmp)
        test_bmp_frames(tmp)
        test_qml_callback_contract(tmp)
        test_debug_diagnostics(tmp)
        test_scanned_key_logic(tmp)
        test_js_port(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        qr_scan.reset_session()
    print("qr selftest: %d passed, %d failed" % (CHECKS[0], CHECKS[1]))
    return 1 if CHECKS[1] else 0


if __name__ == "__main__":
    sys.exit(main())
