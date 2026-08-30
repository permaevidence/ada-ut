#!/bin/bash
# Publish a built .click to the Vercel Blob CDN behind the Ada website.
#   app/ada.permaevidence_<version>_all.click   immutable, long cache
#   app/manifest.json                           consumed by the /app page
# The website rewrites /app/:path* to the blob store, so publishing a new
# click never needs a website deploy — same pattern as the CLI releases.
#
# Usage:  BLOB_TOKEN=... scripts/publish_click.sh [version]
# Version defaults to the one in manifest.json. Get the token with:
#   vercel env pull --environment=development (ada-website project)
set -euo pipefail
cd "$(dirname "$0")/.."

: "${BLOB_TOKEN:?BLOB_TOKEN is required (the ada-website Blob store token)}"
PREFIX="https://z3hrivnareyralos.public.blob.vercel-storage.com/app"

VERSION="${1:-$(python3 -c "import json; print(json.load(open('manifest.json'))['version'])")}"
VERSION="${VERSION#v}"
CLICK="build/ada.permaevidence_${VERSION}_all.click"
[ -f "$CLICK" ] || { echo "✗ $CLICK not found — build it first (scripts/build_click.py)"; exit 1; }

# Supersession guard: never clobber a newer live manifest with an older click.
LIVE_VERSION="$(curl -sf "$PREFIX/manifest.json" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['version'])" 2>/dev/null || echo "")"
if [ -n "$LIVE_VERSION" ]; then
    NEWER="$(python3 - "$LIVE_VERSION" "$VERSION" <<'PYEOF'
import sys
def parse(v):
    try:
        return [int(x) for x in v.split("-")[0].split(".")]
    except ValueError:
        return None
live, mine = parse(sys.argv[1]), parse(sys.argv[2])
if live is None or mine is None:
    print("go")
else:
    n = max(len(live), len(mine))
    live += [0] * (n - len(live)); mine += [0] * (n - len(mine))
    print("skip" if live > mine else "go")
PYEOF
)"
    if [ "$NEWER" = "skip" ]; then
        echo "⚠ CDN already serves $LIVE_VERSION (newer than $VERSION) — skipping publish."
        exit 0
    fi
fi

blob_put() {
    local file="$1" pathname="$2" maxage="$3" encoded
    encoded="$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$pathname")"
    curl -sf -X PUT "https://blob.vercel-storage.com/$encoded" \
        -H "Authorization: Bearer $BLOB_TOKEN" \
        -H "x-api-version: 7" \
        -H "x-add-random-suffix: 0" \
        -H "x-allow-overwrite: 1" \
        -H "x-cache-control-max-age: $maxage" \
        --data-binary "@$file" >/dev/null
    echo "  ↑ $pathname"
}

FILENAME="$(basename "$CLICK")"
SHA256="$(shasum -a 256 "$CLICK" | awk '{print $1}')"
SIZE="$(wc -c < "$CLICK" | tr -d ' ')"

blob_put "$CLICK" "app/$FILENAME" 31536000

MANIFEST="$(mktemp)"
python3 - "$VERSION" "$FILENAME" "$SHA256" "$SIZE" > "$MANIFEST" <<'PYEOF'
import json, sys, datetime
print(json.dumps({
    "version": sys.argv[1],
    "filename": sys.argv[2],
    "sha256": sys.argv[3],
    "size": int(sys.argv[4]),
    "published": datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
}, indent=2))
PYEOF
blob_put "$MANIFEST" "app/manifest.json" 300
rm -f "$MANIFEST"

echo "✔ published $FILENAME ($SIZE bytes, sha256 $SHA256)"
echo "  live at: https://ada-app-psi.vercel.app/app/$FILENAME"
