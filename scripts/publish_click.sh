#!/bin/bash
# Publish a built .click as an IMMUTABLE, SIGNED GitHub Release of
# permaevidence/ada-ut (docs: ada-cli RELEASE_SIGNING_PLAN.md §9.3).
#
#   scripts/publish_click.sh [--bootstrap] [--legacy-blob] [--dry-run]
#                            [--allow-dirty] [version]
#
# Flow (every step fails closed):
#   1. source state: clean tree, version == manifest.json, exact SemVer,
#      sequence == py/release_verify.py APP_RELEASE_SEQUENCE;
#   2. deterministic click build (scripts/build_click.py);
#   3. supersession: the LIVE envelope must authenticate with the committed
#      key and carry a strictly lower sequence — or be absent, which only
#      --bootstrap (the one-time first signed release) may accept; an invalid
#      live envelope is a hard stop, never "absent";
#   4. manifest → sign with the LOCAL app key (never on argv, never printed)
#      → verify with the committed public key AND with the app's own Python
#      verifier (what the phone will run);
#   5. draft release, assets, envelope last, atomic publish (immutable);
#   6. re-download the public envelope + click and require byte identity;
#      require the release to be immutable and non-draft;
#   7. record the publication (outside the repo) only after step 6;
#   8. --legacy-blob: ALSO publish the pre-signature Blob layout so app
#      versions ≤ 0.7.3 can make their last unsigned hop (transition only).
#
# Env: SIGNING_KEY  private key PEM (default ~/.ada-release-keys/<keyId>.priv.pem
#                   where keyId is derived from the committed public key)
#      EXPECTED_PUB committed public key (default .release-keys/ada-ut-release.pub.pem)
#      GH_TOKEN     (default: `gh auth token`), REPO (default permaevidence/ada-ut)
#      GH_API_URL / GH_UPLOADS_URL / PUBLIC_DOWNLOAD_BASE / LIVE_ENVELOPE_URL /
#      BLOB_API_URL / BLOB_PUBLIC_PREFIX / PUBLICATION_LOG / PUBLISHED_AT /
#      EXPIRES_DAYS / REPO_ROOT — overrides for the publisher selftest.
set -euo pipefail

BOOTSTRAP=0; LEGACY_BLOB=0; DRY_RUN=0; ALLOW_DIRTY=0; VERSION_ARG=""
for arg in "$@"; do
    case "$arg" in
        --bootstrap) BOOTSTRAP=1;;
        --legacy-blob) LEGACY_BLOB=1;;
        --dry-run) DRY_RUN=1;;
        --allow-dirty) ALLOW_DIRTY=1;;
        --*) echo "✖ unknown option $arg"; exit 2;;
        *) VERSION_ARG="$arg";;
    esac
done

cd "${REPO_ROOT:-$(dirname "$0")/..}"
REPO="${REPO:-permaevidence/ada-ut}"
API="${GH_API_URL:-https://api.github.com}"
UPLOADS="${GH_UPLOADS_URL:-https://uploads.github.com}"
DOWNLOAD_BASE="${PUBLIC_DOWNLOAD_BASE:-https://github.com/$REPO/releases/download}"
LIVE_URL="${LIVE_ENVELOPE_URL:-https://github.com/$REPO/releases/latest/download/manifest.sig.json}"
EXPECTED_PUB="${EXPECTED_PUB:-.release-keys/ada-ut-release.pub.pem}"
LOG="${PUBLICATION_LOG:-$HOME/.ada-release-keys/ada-ut-publications.jsonl}"
RELEASE_SCRIPTS="scripts/release"
CHANNEL="ada-ut"
TITLE="Ada for Ubuntu Touch"

[ -f "$EXPECTED_PUB" ] || { echo "✖ committed public key missing: $EXPECTED_PUB"; exit 1; }
# shellcheck source=release/openssl-resolve.sh
. "$RELEASE_SCRIPTS/openssl-resolve.sh"
resolve_openssl || { echo "✖ no Ed25519-capable openssl (set OPENSSL_BIN)"; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- keyId from the COMMITTED key; the private key is looked up by it.
"$OPENSSL" pkey -pubin -in "$EXPECTED_PUB" -outform DER > "$WORK/pub.der"
tail -c 32 "$WORK/pub.der" > "$WORK/pub.raw"
PUB_HEX="$(ossl_hex "$WORK/pub.raw")"
FP="$("$OPENSSL" dgst -sha256 -hex < "$WORK/pub.raw" | awk '{print $NF}')"
KEYID="$CHANNEL-release-v1-${FP:0:16}"
SIGNING_KEY="${SIGNING_KEY:-$HOME/.ada-release-keys/$KEYID.priv.pem}"
[ -f "$SIGNING_KEY" ] || { echo "✖ signing key not found: $SIGNING_KEY (custody: plan §4.1)"; exit 1; }
PERM="$(stat -f %Lp "$SIGNING_KEY" 2>/dev/null || stat -c %a "$SIGNING_KEY")"
[ "$PERM" = "600" ] || { echo "✖ signing key $SIGNING_KEY must be mode 0600 (is $PERM)"; exit 1; }

# --- 1. source state
if [ "$ALLOW_DIRTY" = 0 ] && [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    echo "✖ working tree is not clean — commit first (or --allow-dirty for a dry run)"; exit 1
fi
VERSION="$(python3 -c "import json; print(json.load(open('manifest.json'))['version'])")"
if [ -n "$VERSION_ARG" ] && [ "${VERSION_ARG#v}" != "$VERSION" ]; then
    echo "✖ requested version ${VERSION_ARG#v} != manifest.json version $VERSION"; exit 1
fi
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "✖ '$VERSION' is not exact SemVer"; exit 1; }
SEQUENCE="$(grep -E '^APP_RELEASE_SEQUENCE = [0-9]+$' py/release_verify.py | grep -oE '[0-9]+')"
[[ "$SEQUENCE" =~ ^[1-9][0-9]*$ ]] || { echo "✖ APP_RELEASE_SEQUENCE not found in py/release_verify.py"; exit 1; }
grep -q "\"$KEYID\"" py/release_verify.py || {
    echo "✖ py/release_verify.py does not pin $KEYID — the app would refuse its own update"; exit 1; }
TAG="v$VERSION"
echo "release $TAG (sequence $SEQUENCE, key $KEYID)"

# --- 2. deterministic build
python3 scripts/build_click.py >/dev/null
CLICK="build/ada.permaevidence_${VERSION}_all.click"
[ -f "$CLICK" ] || { echo "✖ build did not produce $CLICK"; exit 1; }
FILENAME="$(basename "$CLICK")"
SHA256="$("$OPENSSL" dgst -sha256 -hex < "$CLICK" | awk '{print $NF}')"
SIZE="$(wc -c < "$CLICK" | tr -d ' ')"
echo "  built $FILENAME ($SIZE bytes, sha256 $SHA256)"

# --- 3. supersession against the AUTHENTICATED live channel
LIVE_STATUS="$(curl -sSL --max-filesize 131072 -o "$WORK/live.sig.json" -w '%{http_code}' "$LIVE_URL" 2>/dev/null || echo 000)"
case "$LIVE_STATUS" in
    200)
        "$RELEASE_SCRIPTS/verify-envelope.sh" "$WORK/live.sig.json" "$EXPECTED_PUB" "$CHANNEL" "$WORK/live-payload.json" >/dev/null || {
            echo "✖ the LIVE envelope does not authenticate against the committed key — hard stop (never treated as absent)"; exit 1; }
        LIVE_SEQ="$(python3 -c "import json;print(json.load(open('$WORK/live-payload.json'))['sequence'])")"
        LIVE_VER="$(python3 -c "import json;print(json.load(open('$WORK/live-payload.json'))['version'])")"
        echo "  live: v$LIVE_VER sequence $LIVE_SEQ"
        [ "$SEQUENCE" -gt "$LIVE_SEQ" ] || {
            echo "✖ superseded: sequence $SEQUENCE is not greater than live $LIVE_SEQ — bump APP_RELEASE_SEQUENCE"; exit 1; }
        [ "$BOOTSTRAP" = 0 ] || { echo "✖ --bootstrap given but a live signed release exists"; exit 1; }
        ;;
    404)
        [ "$BOOTSTRAP" = 1 ] || {
            echo "✖ no live signed release reachable at $LIVE_URL — only the one-time first signed release may proceed, with --bootstrap"; exit 1; }
        echo "  live: none (bootstrap accepted for $TAG)"
        ;;
    *)  echo "✖ cannot read the live envelope (HTTP $LIVE_STATUS) — refusing to guess"; exit 1;;
esac

# --- 4. manifest, signature, double verification
DIST="$WORK/dist"; mkdir -p "$DIST"
cp "$CLICK" "$DIST/$FILENAME"
python3 - "$VERSION" "$SEQUENCE" "$DOWNLOAD_BASE/v$VERSION/$FILENAME" "$SHA256" "$SIZE" \
    "${PUBLISHED_AT:-}" "${EXPIRES_DAYS:-180}" > "$DIST/manifest.json" <<'PYEOF'
import datetime, json, sys
version, sequence, url, sha, size, published_at, days = sys.argv[1:8]
now = (datetime.datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
       if published_at else datetime.datetime.now(datetime.timezone.utc))
fmt = "%Y-%m-%dT%H:%M:%SZ"
print(json.dumps({
    "channel": "ada-ut",
    "expires": (now + datetime.timedelta(days=int(days))).strftime(fmt),
    "platforms": {"click": {"sha256": sha, "size": int(size), "url": url}},
    "published": now.strftime(fmt),
    "schema": 1,
    "sequence": int(sequence),
    "version": version,
}, indent=2, sort_keys=True))
PYEOF
EXPECTED_PUBKEY_PEM="$EXPECTED_PUB" "$RELEASE_SCRIPTS/sign-envelope.sh" \
    "$SIGNING_KEY" "$CHANNEL" "$DIST/manifest.json" "$DIST/manifest.sig.json" >/dev/null
"$RELEASE_SCRIPTS/verify-envelope.sh" "$DIST/manifest.sig.json" "$EXPECTED_PUB" "$CHANNEL" "$WORK/payload.json" >/dev/null
cmp -s "$WORK/payload.json" "$DIST/manifest.json" || { echo "✖ verified payload differs from the manifest"; exit 1; }
# The phone's own verifier must accept exactly this envelope under the
# production policy shape (committed key, this download location).
python3 - "$DIST/manifest.sig.json" "$KEYID" "$PUB_HEX" "$DOWNLOAD_BASE" "$SEQUENCE" "$VERSION" "$SHA256" "$SIZE" <<'PYEOF'
import sys, os
sys.path.insert(0, "py")
import release_verify as rv
env_path, key_id, pub_hex, base, seq, version, sha, size = sys.argv[1:9]
policy = rv.ReleasePolicy("ada-ut", {key_id: pub_hex},
                          base + "/latest/manifest.sig.json", base + "/v{version}/", 1)
m = rv.verify_envelope(open(env_path, "rb").read(), policy)
entry = m["platforms"]["click"]
assert (m["sequence"], m["version"]) == (int(seq), version), "sequence/version mismatch"
assert entry["sha256"] == sha and entry["size"] == int(size), "asset metadata mismatch"
assert entry["filename"].endswith(".click"), "asset is not a click"
print("  ✔ the app's verifier (%s) accepts the envelope" % rv.provider()[0])
PYEOF
ENVELOPE_SHA="$("$OPENSSL" dgst -sha256 -hex < "$DIST/manifest.sig.json" | awk '{print $NF}')"

if [ "$DRY_RUN" = 1 ]; then
    echo "✔ dry run: $TAG sequence $SEQUENCE signed and verified; nothing published"
    exit 0
fi

# --- 5. immutable GitHub release (draft → assets → envelope last → publish)
GH_TOKEN="${GH_TOKEN:-$(gh auth token 2>/dev/null || true)}"
[ -n "$GH_TOKEN" ] || { echo "✖ GH_TOKEN is required (or gh auth login)"; exit 1; }
export GH_TOKEN
RELEASE_ID_OUT="$WORK/release-id" \
ASSETS="$DIST/$FILENAME
$DIST/manifest.json
$DIST/manifest.sig.json" \
REPO="$REPO" REF_NAME="$TAG" VERSION="$VERSION" TITLE="$TITLE" \
GH_API_URL="$API" GH_UPLOADS_URL="$UPLOADS" \
    "$RELEASE_SCRIPTS/publish-github-release.sh"
RELEASE_ID="$(cat "$WORK/release-id")"

# --- 6. re-verify the PUBLIC state, byte for byte
fetch_public() {  # url out — `latest` can lag a few seconds after publish
    local url="$1" out="$2" attempt status
    for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
        status="$(curl -sSL --max-filesize 33554432 -o "$out" -w '%{http_code}' "$url" 2>/dev/null || echo 000)"
        [ "$status" = "200" ] && return 0
        sleep "${PUBLIC_RETRY_SLEEP:-5}"
    done
    echo "✖ public fetch of $url failed (last HTTP $status)"; return 1
}
fetch_public "$LIVE_URL" "$WORK/public.sig.json"
cmp -s "$WORK/public.sig.json" "$DIST/manifest.sig.json" || {
    echo "✖ the public LATEST envelope is not byte-identical to what was just signed — investigate before anything else"; exit 1; }
fetch_public "$DOWNLOAD_BASE/v$VERSION/$FILENAME" "$WORK/public.click"
cmp -s "$WORK/public.click" "$CLICK" || { echo "✖ the public click differs from the built one"; exit 1; }
REL_STATUS="$(curl -sS -o "$WORK/rel.json" -w '%{http_code}' -H "Authorization: Bearer $GH_TOKEN" \
    -H "Accept: application/vnd.github+json" "$API/repos/$REPO/releases/tags/$TAG" 2>/dev/null || echo 000)"
[ "$REL_STATUS" = "200" ] || { echo "✖ cannot read the published release (HTTP $REL_STATUS)"; exit 1; }
python3 - "$WORK/rel.json" "$RELEASE_ID" <<'PYEOF'
import json, sys
rel = json.load(open(sys.argv[1]))
assert str(rel.get("id")) == sys.argv[2], "release id mismatch"
assert rel.get("draft") is False, "release is still a draft"
assert rel.get("immutable") is True, "release is NOT immutable — enable immutable releases on the repository"
PYEOF
echo "✔ public state verified: $TAG immutable, envelope + click byte-identical"

# --- 7. record (outside the repository) only after verification
mkdir -p "$(dirname "$LOG")"
python3 - "$LOG" "$TAG" "$VERSION" "$SEQUENCE" "$SHA256" "$ENVELOPE_SHA" "$RELEASE_ID" "$KEYID" <<'PYEOF'
import datetime, json, sys
log, tag, version, seq, sha, env_sha, rid, key = sys.argv[1:9]
with open(log, "a") as f:
    f.write(json.dumps({"tag": tag, "version": version, "sequence": int(seq), "clickSha256": sha,
                        "envelopeSha256": env_sha, "releaseId": int(rid), "keyId": key,
                        "recorded": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}) + "\n")
PYEOF
echo "  recorded in $LOG"

# --- 8. transition only: the pre-signature Blob layout for apps ≤ 0.7.3
if [ "$LEGACY_BLOB" = 1 ]; then
    : "${BLOB_TOKEN:?BLOB_TOKEN is required for --legacy-blob}"
    BLOB_API="${BLOB_API_URL:-https://blob.vercel-storage.com}"
    PREFIX="${BLOB_PUBLIC_PREFIX:-https://z3hrivnareyralos.public.blob.vercel-storage.com/app}"
    LIVE_LEGACY="$(curl -sf "$PREFIX/manifest.json" | python3 -c "import json,sys; print(json.load(sys.stdin)['version'])" 2>/dev/null || echo "")"
    SKIP="$(python3 - "$LIVE_LEGACY" "$VERSION" <<'PYEOF'
import sys
def parse(v):
    try: return [int(x) for x in v.split("-")[0].split(".")]
    except ValueError: return None
live, mine = parse(sys.argv[1]), parse(sys.argv[2])
print("skip" if live and mine and live > mine else "go")
PYEOF
)"
    if [ "$SKIP" = "skip" ]; then
        echo "⚠ legacy Blob already serves $LIVE_LEGACY (newer than $VERSION) — legacy layout left alone"
    else
        blob_put() {
            local file="$1" pathname="$2" maxage="$3" encoded
            encoded="$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$pathname")"
            curl -sf -X PUT "$BLOB_API/$encoded" -H "Authorization: Bearer $BLOB_TOKEN" \
                -H "x-api-version: 7" -H "x-add-random-suffix: 0" -H "x-allow-overwrite: 1" \
                -H "x-cache-control-max-age: $maxage" --data-binary "@$file" >/dev/null
            echo "  ↑ blob $pathname"
        }
        blob_put "$CLICK" "app/$FILENAME" 31536000
        python3 - "$VERSION" "$FILENAME" "$SHA256" "$SIZE" > "$WORK/legacy-manifest.json" <<'PYEOF'
import json, sys, datetime
print(json.dumps({"version": sys.argv[1], "filename": sys.argv[2], "sha256": sys.argv[3],
                  "size": int(sys.argv[4]),
                  "published": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}, indent=2))
PYEOF
        blob_put "$WORK/legacy-manifest.json" "app/manifest.json" 300
        echo "✔ legacy Blob layout published (apps ≤ 0.7.3 can hop to $VERSION)"
    fi
fi

echo "✔ $TAG is live: $DOWNLOAD_BASE/v$VERSION/$FILENAME"
