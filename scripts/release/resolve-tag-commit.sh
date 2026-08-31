#!/bin/bash
# Resolve what a Git TAG actually points at on GitHub, through the Git
# References API — not through the Releases API, whose target_commitish is
# only a creation hint that GitHub silently ignores when the tag already
# exists (https://docs.github.com/rest/releases/releases#create-a-release).
#
#   resolve-tag-commit.sh <owner/repo> <tag>
#
# Env: GH_TOKEN (required), GH_API_URL (default https://api.github.com)
#
# stdout (exactly one line):
#   <40-hex commit sha>  the tag exists; annotated tags are followed (bounded)
#                        down to the commit they ultimately name
#   NONE                 the tag does not exist (HTTP 404 on the exact ref)
# exit 1 on anything else — transport errors, a non-exact ref answer, an
# unexpected object type, an unresolvable annotated tag. Callers must treat
# exit 1 as "refuse", never as "absent".
set -euo pipefail
REPO="${1:?owner/repo required}"
TAG="${2:?tag required}"
: "${GH_TOKEN:?GH_TOKEN is required}"
API="${GH_API_URL:-https://api.github.com}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

api_get() {  # url → STATUS, BODY_FILE
    BODY_FILE="$WORK/body.$RANDOM"
    STATUS="$(curl -sS -o "$BODY_FILE" -w '%{http_code}' \
        -H "Authorization: Bearer $GH_TOKEN" \
        -H "Accept: application/vnd.github+json" \
        "$1" 2>/dev/null || echo 000)"
}

# The singular /git/ref/ endpoint answers one reference; we still require
# the answered ref name to be EXACTLY ours (never a prefix match).
api_get "$API/repos/$REPO/git/ref/tags/$TAG"
case "$STATUS" in
    404) echo NONE; exit 0;;
    200) ;;
    *)   echo "✖ cannot resolve refs/tags/$TAG (HTTP $STATUS) — refusing to guess" >&2; exit 1;;
esac
RESOLVED="$(python3 - "$BODY_FILE" "refs/tags/$TAG" <<'PYEOF'
import json, re, sys
ref = json.load(open(sys.argv[1]))
if isinstance(ref, list):
    sys.exit("✖ reference lookup returned a list (prefix match) instead of one exact ref")
if ref.get("ref") != sys.argv[2]:
    sys.exit("✖ reference lookup answered %r, not %r" % (ref.get("ref"), sys.argv[2]))
obj = ref.get("object") or {}
sha = str(obj.get("sha", ""))
if obj.get("type") not in ("commit", "tag") or not re.fullmatch(r"[0-9a-f]{40}", sha):
    sys.exit("✖ unexpected ref object: %r" % (obj,))
print(obj["type"], sha)
PYEOF
)" || exit 1
read -r OBJ_TYPE OBJ_SHA <<< "$RESOLVED"

# Follow annotated tags (a tag object may itself name another tag object).
depth=0
while [ "$OBJ_TYPE" = "tag" ]; do
    depth=$((depth + 1))
    [ "$depth" -le 5 ] || { echo "✖ refs/tags/$TAG: annotated-tag chain deeper than 5 — refusing" >&2; exit 1; }
    api_get "$API/repos/$REPO/git/tags/$OBJ_SHA"
    [ "$STATUS" = "200" ] || { echo "✖ cannot read annotated tag object $OBJ_SHA (HTTP $STATUS)" >&2; exit 1; }
    RESOLVED="$(python3 - "$BODY_FILE" <<'PYEOF'
import json, re, sys
obj = (json.load(open(sys.argv[1])).get("object")) or {}
sha = str(obj.get("sha", ""))
if obj.get("type") not in ("commit", "tag") or not re.fullmatch(r"[0-9a-f]{40}", sha):
    sys.exit("✖ unexpected annotated-tag target: %r" % (obj,))
print(obj["type"], sha)
PYEOF
)" || exit 1
    read -r OBJ_TYPE OBJ_SHA <<< "$RESOLVED"
done
[ "$OBJ_TYPE" = "commit" ] || { echo "✖ refs/tags/$TAG does not resolve to a commit" >&2; exit 1; }
echo "$OBJ_SHA"
