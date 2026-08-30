#!/usr/bin/env python3
"""Click packaging regression test — inspects the built artifact itself.

Builds the .click with scripts/build_click.py and verifies, by parsing the
ar archive and its data.tar.gz (never by trusting the builder's file list):

  1. LICENSE ships inside the data area, byte-identical to the repo's
     LICENSE (BUSL 1.1 requires the license text to accompany copies).
  2. manifest.json ships in the data area.
  3. No build artifacts leak in (__pycache__, .pyc/.pyo, .DS_Store).
  4. md5sums covers LICENSE with the correct digest.
  5. Two builds are byte-identical (deterministic packaging).

Usage: python3 scripts/click_selftest.py
"""

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append(ok)
    print("%s %s%s" % ("✓" if ok else "✗", name, (" — " + detail) if detail and not ok else ""))


def parse_ar(blob):
    """Common-format ar archive -> {member_name: payload_bytes}."""
    assert blob[:8] == b"!<arch>\n", "not an ar archive"
    members = {}
    off = 8
    while off < len(blob):
        header = blob[off:off + 60]
        if len(header) < 60:
            break
        name = header[0:16].decode("ascii").strip()
        size = int(header[48:58].decode("ascii").strip())
        payload = blob[off + 60:off + 60 + size]
        members[name] = payload
        off += 60 + size + (size % 2)
    return members


def build(out_dir):
    subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "build_click.py"),
                    out_dir], check=True, capture_output=True)
    clicks = [f for f in os.listdir(out_dir) if f.endswith(".click")]
    assert len(clicks) == 1, clicks
    with open(os.path.join(out_dir, clicks[0]), "rb") as f:
        return f.read()


def main():
    with tempfile.TemporaryDirectory() as td:
        blob = build(os.path.join(td, "a"))
        blob2 = build(os.path.join(td, "b"))

    check("deterministic build (two runs byte-identical)", blob == blob2)

    members = parse_ar(blob)
    check("ar members present",
          {"debian-binary", "control.tar.gz", "data.tar.gz"} <= set(members),
          "got: %s" % sorted(members))

    with tarfile.open(fileobj=io.BytesIO(members["data.tar.gz"]), mode="r:gz") as tar:
        data_names = tar.getnames()
        license_payload = tar.extractfile("./LICENSE").read() if "./LICENSE" in data_names else None
        manifest_payload = tar.extractfile("./manifest.json").read() if "./manifest.json" in data_names else None

    with open(os.path.join(ROOT, "LICENSE"), "rb") as f:
        repo_license = f.read()
    check("LICENSE ships in data.tar.gz", license_payload is not None,
          "members: %s" % data_names[:10])
    check("shipped LICENSE is byte-identical to repo LICENSE",
          license_payload == repo_license)
    check("shipped LICENSE is the BUSL text",
          license_payload is not None and b"Business Source License 1.1" in license_payload)
    check("manifest.json ships in data.tar.gz", manifest_payload is not None)
    if manifest_payload:
        json.loads(manifest_payload)  # must be valid JSON
        check("shipped manifest.json parses", True)

    leaks = [n for n in data_names
             if "__pycache__" in n or n.endswith((".pyc", ".pyo")) or n.endswith(".DS_Store")]
    check("no build artifacts in data area", not leaks, str(leaks))

    with tarfile.open(fileobj=io.BytesIO(members["control.tar.gz"]), mode="r:gz") as tar:
        md5sums = tar.extractfile("./md5sums").read().decode()
    expected = "%s  LICENSE" % hashlib.md5(repo_license).hexdigest()
    check("md5sums covers LICENSE with the correct digest", expected in md5sums)

    passed = sum(CHECKS)
    print("\n%d/%d checks passed" % (passed, len(CHECKS)))
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
