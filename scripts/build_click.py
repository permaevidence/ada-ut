#!/usr/bin/env python3
"""Build the briglia-ut .click package — no Clickable, no Docker, no dpkg.

The app is pure QML + Python (architecture "all": nothing to compile), and a
.click is just a Debian-format ar archive (debian-binary + control.tar.gz +
data.tar.gz) with click metadata in the control member. This script builds
it deterministically with the Python standard library, so the same command
works on the Mac and in CI.

Usage:  python3 scripts/build_click.py [output-dir]
Output: <output-dir or build/>/briglia.permaevidence_<version>_all.click
"""

import gzip
import hashlib
import io
import json
import os
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Everything shipped inside the click's data area, relative to the repo root.
# LICENSE must ship with every distributed copy (BUSL 1.1 requires the
# license text to accompany copies) — scripts/click_selftest.py enforces it.
DATA_ITEMS = ["click", "qml", "py", "assets", "LICENSE"]

# Build artifacts that must never ship (a Mac dev run would otherwise
# package __pycache__/*.pyc — Codex caught one in the first build).
EXCLUDED_DIRS = {"__pycache__"}
EXCLUDED_SUFFIXES = (".pyc", ".pyo")
EXCLUDED_NAMES = {".DS_Store"}

# Deterministic timestamps: honor SOURCE_DATE_EPOCH (reproducible-builds
# convention), default to 0. Two builds of the same tree are byte-identical.
EPOCH = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))

PREINST = b"""#! /bin/sh
echo "Click packages may not be installed directly using dpkg."
echo "Use 'click install' instead."
exit 1
"""


def load_manifest():
    with open(os.path.join(ROOT, "manifest.json"), "rb") as f:
        return json.loads(f.read())


def iter_data_files():
    for item in DATA_ITEMS:
        base = os.path.join(ROOT, item)
        if os.path.isfile(base):
            yield item
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
            for name in sorted(filenames):
                if name in EXCLUDED_NAMES or name.endswith(EXCLUDED_SUFFIXES):
                    continue
                full = os.path.join(dirpath, name)
                yield os.path.relpath(full, ROOT)
    yield "manifest.json"  # click also ships the manifest in the data area


def make_tar(entries):
    """entries: list of (archive_name, bytes, mode). Returns gz bytes
    (deterministic: fixed member mtimes, gzip header mtime 0, no filename)."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        # Parent directories first, once each.
        seen_dirs = set()
        for name, _, _ in entries:
            parts = name.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                d = "/".join(parts[:i])
                if d and d not in seen_dirs:
                    seen_dirs.add(d)
        for d in sorted(seen_dirs):
            info = tarfile.TarInfo("./" + d + "/")
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = EPOCH
            info.uname = info.gname = "root"
            tar.addfile(info)
        for name, payload, mode in entries:
            info = tarfile.TarInfo("./" + name)
            info.size = len(payload)
            info.mode = mode
            info.mtime = EPOCH
            info.uname = info.gname = "root"
            tar.addfile(info, io.BytesIO(payload))
    sink = io.BytesIO()
    with gzip.GzipFile(fileobj=sink, mode="wb", mtime=EPOCH) as gz:
        gz.write(raw.getvalue())
    return sink.getvalue()


def ar_member(name, payload):
    """One member of a common-format ar archive (short names only)."""
    header = "{:<16}{:<12}{:<6}{:<6}{:<8}{:<10}`\n".format(
        name, EPOCH, 0, 0, "100644", len(payload)).encode("ascii")
    body = payload if len(payload) % 2 == 0 else payload + b"\n"
    return header + body


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "build")
    os.makedirs(out_dir, exist_ok=True)
    manifest = load_manifest()

    data_entries = []
    md5_lines = []
    for rel in iter_data_files():
        with open(os.path.join(ROOT, rel), "rb") as f:
            payload = f.read()
        mode = 0o755 if os.access(os.path.join(ROOT, rel), os.X_OK) else 0o644
        data_entries.append((rel, payload, mode))
        md5_lines.append("%s  %s" % (hashlib.md5(payload).hexdigest(), rel))
    data_tar = make_tar(data_entries)

    installed_size = sum(len(p) for _, p, _ in data_entries) // 1024 + 1
    control_text = (
        "Package: %s\n"
        "Version: %s\n"
        "Click-Version: 0.4\n"
        "Architecture: %s\n"
        "Maintainer: %s\n"
        "Installed-Size: %d\n"
        "Description: %s\n"
        % (manifest["name"], manifest["version"], manifest["architecture"],
           manifest["maintainer"], installed_size, manifest["description"]))
    control_entries = [
        ("control", control_text.encode(), 0o644),
        ("manifest", json.dumps(manifest, indent=4).encode() + b"\n", 0o644),
        ("md5sums", ("\n".join(md5_lines) + "\n").encode(), 0o644),
        ("preinst", PREINST, 0o755),
    ]
    control_tar = make_tar(control_entries)

    click_name = "%s_%s_%s.click" % (
        manifest["name"], manifest["version"], manifest["architecture"])
    out_path = os.path.join(out_dir, click_name)
    with open(out_path, "wb") as out:
        out.write(b"!<arch>\n")
        out.write(ar_member("debian-binary", b"2.0\n"))
        out.write(ar_member("control.tar.gz", control_tar))
        out.write(ar_member("data.tar.gz", data_tar))
    print("built %s (%d KB)" % (out_path, os.path.getsize(out_path) // 1024))


if __name__ == "__main__":
    main()
