#!/bin/bash
# Publish ONE immutable GitHub Release for a tag: assemble a draft, upload
# every asset with the signed envelope LAST, then flip the draft live in a
# single PATCH with explicit make_latest. Generic twin of ada-cli's
# .github/scripts/publish-release.sh (same logic, assets passed in), used by
# scripts/publish_click.sh and exercised against a fake Releases API by
# scripts/publish_selftest.py.
#
# Env (required): GH_TOKEN, REPO (owner/name), REF_NAME (tag), VERSION,
#                 TITLE, ASSETS (newline-separated file paths; the LAST one
#                 must be the signed envelope manifest.sig.json)
# Env (optional): GH_API_URL (default https://api.github.com),
#                 GH_UPLOADS_URL (default https://uploads.github.com)
#
# Exit 0 only when the release is CONFIRMED published (re-read from the API,
# draft=false). An ambiguous PATCH is re-checked: live → success; still a
# draft → reported failure, the draft is left for the next run's cleanup.
# The token only ever travels in a request header, never echoed.
set -euo pipefail
: "${GH_TOKEN:?GH_TOKEN is required}"
: "${REPO:?REPO is required}"
: "${REF_NAME:?REF_NAME is required}"
: "${VERSION:?VERSION is required}"
: "${TITLE:?TITLE is required}"
: "${ASSETS:?ASSETS is required}"
API="${GH_API_URL:-https://api.github.com}"
UPLOADS="${GH_UPLOADS_URL:-https://uploads.github.com}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

api() {
    local method="$1" url="$2"; shift 2
    BODY_FILE="$WORK/body.$$.$RANDOM"
    STATUS="$(curl -sS -o "$BODY_FILE" -w '%{http_code}' -X "$method" \
        -H "Authorization: Bearer $GH_TOKEN" \
        -H "Accept: application/vnd.github+json" \
        "$@" "$url" 2>/dev/null || echo 000)"
}
jget() { python3 -c 'import json,sys; v=json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."): v=v[k]
print(v)' "$1" "$2"; }

# Every asset must exist before anything touches the API, envelope last.
LAST=""
while IFS= read -r asset; do
    [ -n "$asset" ] || continue
    [ -f "$asset" ] || { echo "✖ asset missing: $asset"; exit 1; }
    LAST="$asset"
done <<< "$ASSETS"
[ "$(basename "$LAST")" = "manifest.sig.json" ] || {
    echo "✖ the last asset must be the signed envelope manifest.sig.json (got $(basename "$LAST"))"; exit 1; }

# 1. A PUBLISHED release for this tag is immutable — republish is a hard
#    error, never a retry loop. (This endpoint never returns drafts.)
api GET "$API/repos/$REPO/releases/tags/$REF_NAME"
case "$STATUS" in
    200) echo "✖ a published release for $REF_NAME already exists — immutability forbids republish; cut a new version"; exit 1;;
    404) ;;
    *)   echo "✖ cannot determine whether $REF_NAME is already published (HTTP $STATUS) — refusing to guess"; exit 1;;
esac

# 2. Stale DRAFTS from a previously failed run are not immutable — remove
#    them so retries are idempotent.
page=1
while :; do
    api GET "$API/repos/$REPO/releases?per_page=100&page=$page"
    [ "$STATUS" = "200" ] || { echo "✖ listing releases failed (HTTP $STATUS)"; exit 1; }
    STALE="$(python3 -c 'import json,sys
rel=json.load(open(sys.argv[1])); tag=sys.argv[2]
print("\n".join(str(r["id"]) for r in rel if r.get("draft") and r.get("tag_name")==tag))
print("MORE" if len(rel)==100 else "END", file=sys.stderr)' "$BODY_FILE" "$REF_NAME" 2>"$WORK/more")"
    for stale in $STALE; do
        echo "deleting stale draft $stale"
        api DELETE "$API/repos/$REPO/releases/$stale"
        [ "$STATUS" = "204" ] || { echo "✖ deleting stale draft $stale failed (HTTP $STATUS)"; exit 1; }
    done
    [ "$(cat "$WORK/more")" = "MORE" ] || break
    page=$((page + 1))
done

# 3. Create the draft and address it by ID from here on (a draft cannot be
#    resolved by tag).
python3 -c 'import json,sys
print(json.dumps({"tag_name": sys.argv[1], "draft": True, "name": sys.argv[2] + " " + sys.argv[3],
  "body": "Signed release " + sys.argv[3] + ". Clients authenticate manifest.sig.json with the pinned Ed25519 key before trusting any asset."}))' \
    "$REF_NAME" "$TITLE" "$VERSION" > "$WORK/create.json"
api POST "$API/repos/$REPO/releases" -H "Content-Type: application/json" --data-binary @"$WORK/create.json"
[ "$STATUS" = "201" ] || { echo "✖ creating the draft release failed (HTTP $STATUS)"; exit 1; }
RELEASE_ID="$(jget "$BODY_FILE" id)"
[ "$(jget "$BODY_FILE" draft)" = "True" ] || { echo "✖ created release is not a draft — refusing to continue"; exit 1; }
echo "draft release $RELEASE_ID created for $REF_NAME"

# 4. Assets in order, the signed envelope LAST — stable metadata cannot
#    precede what it describes even inside the draft.
upload() {
    local file="$1" name
    name="$(basename "$file")"
    api POST "$UPLOADS/repos/$REPO/releases/$RELEASE_ID/assets?name=$name" \
        -H "Content-Type: application/octet-stream" --data-binary @"$file"
    [ "$STATUS" = "201" ] || {
        echo "✖ uploading $name failed (HTTP $STATUS) — draft $RELEASE_ID left unpublished; the old release stays latest"; exit 1; }
    echo "  ↑ $name"
}
while IFS= read -r asset; do
    [ -n "$asset" ] || continue
    upload "$asset"
done <<< "$ASSETS"

# 5. Atomic go-live; immutability locks at this moment. make_latest is
#    explicit — never GitHub's default.
api PATCH "$API/repos/$REPO/releases/$RELEASE_ID" -H "Content-Type: application/json" \
    --data-binary '{"draft":false,"make_latest":"true"}'
PATCH_STATUS="$STATUS"

# 6. Confirm from the API, whatever the PATCH said: only an observed
#    draft=false is success. (A PATCH can time out AFTER GitHub applied it.)
api GET "$API/repos/$REPO/releases/$RELEASE_ID"
if [ "$STATUS" = "200" ] && [ "$(jget "$BODY_FILE" draft)" = "False" ] \
   && [ "$(jget "$BODY_FILE" tag_name)" = "$REF_NAME" ]; then
    if [ "$PATCH_STATUS" != "200" ]; then
        echo "⚠ publish PATCH answered HTTP $PATCH_STATUS but the release IS published (confirmed by re-read)"
    fi
    echo "✔ published immutable release $REF_NAME (id $RELEASE_ID)"
    echo "$RELEASE_ID" > "${RELEASE_ID_OUT:-/dev/null}"
    exit 0
fi
echo "✖ draft publication FAILED (PATCH HTTP $PATCH_STATUS, re-read HTTP $STATUS) — release $RELEASE_ID remains a draft; the old release stays latest"
exit 1
