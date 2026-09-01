#!/bin/bash
# Publish a built .click as an IMMUTABLE, SIGNED GitHub Release of
# permaevidence/briglia-ut (docs: briglia-cli RELEASE_SIGNING_PLAN.md §9.3).
#
#   scripts/publish_click.sh [--dry-run] [--allow-dirty] [version]
#
# Flow (every step fails closed):
#   1. source state: clean tree, version == manifest.json, exact SemVer,
#      sequence == py/release_verify.py APP_RELEASE_SEQUENCE;
#   2. deterministic click build (scripts/build_click.py);
#   3. supersession: the LIVE envelope must authenticate with the committed
#      key and carry a strictly lower sequence. An absent live envelope is a
#      refusal — the signed app channel was bootstrapped once (v0.7.4,
#      2026-08-31) and never restarts from nothing; an invalid live envelope
#      is a hard stop, never "absent". Rename transition (RENAME_PLAN.md
#      §3.2): until the first Briglia release publishes, the renamed
#      repository's "latest" is still the previous identity's envelope; a
#      compiled legacy descriptor below accepts EXACTLY that state, for the
#      transition release only;
#   4. manifest → sign with the LOCAL app key (never on argv, never printed)
#      → verify with the committed public key AND with the app's own Python
#      verifier (what the phone will run);
#   5. draft release, assets, envelope last, atomic publish (immutable);
#   6. re-download the public envelope + click and require byte identity;
#      require the release to be immutable and non-draft;
#   7. record the publication (outside the repo) only after step 6.
#
# The pre-signature Vercel Blob layout (apps ≤ 0.7.3) was retired on
# 2026-08-31 once every device was on 0.7.4; GitHub Releases is the only
# distribution channel.
#
# Concurrency: the whole run holds an exclusive cross-process lock (outside
# the repository; a second publisher is refused, not queued), and the
# authenticated supersession check is repeated immediately before the
# release is created. The tag is bound to the exact reviewed HEAD commit
# (which must already be on origin/main), never "whatever main is now":
# refs/tags/v<version> is resolved through the Git References API (annotated
# tags followed) before the draft, before publication and after publication
# — a pre-existing tag naming any other commit is a refusal, because GitHub
# keeps an existing tag and silently ignores target_commitish.
#
# Env: SIGNING_KEY  private key PEM (default ~/.briglia-release-keys/<keyId>.priv.pem
#                   where keyId is derived from the committed public key)
#      EXPECTED_PUB committed public key (default .release-keys/briglia-ut-release.pub.pem)
#      GH_TOKEN     (default: `gh auth token`), REPO (default permaevidence/briglia-ut)
#      GH_API_URL / GH_UPLOADS_URL / PUBLIC_DOWNLOAD_BASE / LIVE_ENVELOPE_URL /
#      PUBLICATION_LOG / PUBLISHED_AT / EXPIRES_DAYS / REPO_ROOT /
#      PUBLISH_LOCK — overrides for the selftest.
set -euo pipefail

# --- exclusive publisher lock (re-exec under a python flock holder; the
# lock fd is inherited across exec and released when this process exits).
if [ -z "${BRIGLIA_UT_PUBLISH_LOCKED:-}" ]; then
    export BRIGLIA_UT_PUBLISH_LOCK_PATH="${PUBLISH_LOCK:-$HOME/.briglia-release-keys/briglia-ut-publish.lock}"
    exec python3 - "$0" "$@" <<'PYEOF'
import fcntl, os, sys
script, args = sys.argv[1], sys.argv[2:]
path = os.environ["BRIGLIA_UT_PUBLISH_LOCK_PATH"]
os.makedirs(os.path.dirname(path), exist_ok=True)
fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("✖ another publish_click.sh run holds the publisher lock (%s) — refusing to run concurrently" % path)
    sys.exit(1)
os.set_inheritable(fd, True)
os.environ["BRIGLIA_UT_PUBLISH_LOCKED"] = "1"
os.execv("/bin/bash", ["/bin/bash", script] + args)
PYEOF
fi

DRY_RUN=0; ALLOW_DIRTY=0; VERSION_ARG=""
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1;;
        --allow-dirty) ALLOW_DIRTY=1;;
        --*) echo "✖ unknown option $arg"; exit 2;;
        *) VERSION_ARG="$arg";;
    esac
done

cd "${REPO_ROOT:-$(dirname "$0")/..}"
REPO="${REPO:-permaevidence/briglia-ut}"
API="${GH_API_URL:-https://api.github.com}"
UPLOADS="${GH_UPLOADS_URL:-https://uploads.github.com}"
DOWNLOAD_BASE="${PUBLIC_DOWNLOAD_BASE:-https://github.com/$REPO/releases/download}"
LIVE_URL="${LIVE_ENVELOPE_URL:-https://github.com/$REPO/releases/latest/download/manifest.sig.json}"
EXPECTED_PUB="${EXPECTED_PUB:-.release-keys/briglia-ut-release.pub.pem}"
LOG="${PUBLICATION_LOG:-$HOME/.briglia-release-keys/briglia-ut-publications.jsonl}"
RELEASE_SCRIPTS="scripts/release"
CHANNEL="briglia-ut"
TITLE="Briglia for Ubuntu Touch"

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
SIGNING_KEY="${SIGNING_KEY:-$HOME/.briglia-release-keys/$KEYID.priv.pem}"
[ -f "$SIGNING_KEY" ] || { echo "✖ signing key not found: $SIGNING_KEY (custody: plan §4.1)"; exit 1; }
PERM="$(stat -f %Lp "$SIGNING_KEY" 2>/dev/null || stat -c %a "$SIGNING_KEY")"
[ "$PERM" = "600" ] || { echo "✖ signing key $SIGNING_KEY must be mode 0600 (is $PERM)"; exit 1; }

# --- legacy-transition descriptor (RENAME_PLAN.md §3.2 / §5). After the
# repository rename, GitHub's "latest" is still the previous identity's
# envelope until the first Briglia click publishes: channel `ada-ut`,
# sequence 1, version 0.7.4, keyId `ada-ut-release-v1-…` over the SAME key
# material, click under the old repository path. It is accepted as the
# supersession floor ONLY when it authenticates with the committed key under
# the old channel domain AND matches this descriptor EXACTLY AND the release
# being published is the transition release (sequence LEGACY+1 = 2). No
# environment override, no bypass flag. This block is deleted in the
# follow-up commit once v0.8.0 is live (a later release supersedes a
# briglia-ut envelope, so the path is inert by construction from then on).
LEGACY_CHANNEL="ada-ut"
LEGACY_SEQUENCE=1
LEGACY_VERSION="0.7.4"
LEGACY_ARTIFACT_PREFIX="https://github.com/permaevidence/ada-ut/releases/download/v"

# Exact-descriptor check of an AUTHENTICATED legacy payload: field for field
# the genuine pre-rename channel state, nothing else (not a wildcard).
legacy_payload_matches() {
    python3 - "$1" "$LEGACY_CHANNEL" "$LEGACY_SEQUENCE" "$LEGACY_VERSION" "$LEGACY_ARTIFACT_PREFIX" <<'PYCHECK'
import json, sys
path, channel, sequence, version, prefix = sys.argv[1:6]
try:
    m = json.load(open(path))
except Exception:
    sys.exit("✖ legacy envelope authenticates but its payload is not JSON — refusing")
if not isinstance(m, dict) or m.get("schema") != 1 or m.get("channel") != channel:
    sys.exit("✖ legacy envelope authenticates but its payload is not a %s schema-1 manifest — refusing" % channel)
if m.get("sequence") != int(sequence) or m.get("version") != version:
    sys.exit("✖ legacy envelope is not the compiled pre-rename state (sequence %s, version %s): got sequence %r version %r — refusing"
             % (sequence, version, m.get("sequence"), m.get("version")))
platforms = m.get("platforms")
if not isinstance(platforms, dict) or set(platforms) != {"click"}:
    sys.exit("✖ legacy manifest is malformed (expected exactly one 'click' platform) — refusing")
entry = platforms["click"]
url = entry.get("url") if isinstance(entry, dict) else None
if not isinstance(url, str) or not url.startswith(prefix + version + "/"):
    sys.exit("✖ legacy manifest click is not under %s%s/ — refusing" % (prefix, version))
PYCHECK
}

# --- authenticated live-channel read: sets LIVE_STATUS, LIVE_KIND
# (current|legacy), LIVE_SEQ, LIVE_VER, LIVE_PAYLOAD (path). Anything served
# that does not authenticate is a hard stop — never "absent".
read_live() {
    LIVE_STATUS="$(curl -sSL --max-filesize 131072 -o "$WORK/live.sig.json" -w '%{http_code}' "$LIVE_URL" 2>/dev/null || echo 000)"
    LIVE_SEQ=""; LIVE_VER=""; LIVE_KIND=""; LIVE_PAYLOAD="$WORK/live-payload.json"
    case "$LIVE_STATUS" in
        200)
            if "$RELEASE_SCRIPTS/verify-envelope.sh" "$WORK/live.sig.json" "$EXPECTED_PUB" "$CHANNEL" "$LIVE_PAYLOAD" >/dev/null 2>&1; then
                LIVE_KIND="current"
            elif "$RELEASE_SCRIPTS/verify-envelope.sh" "$WORK/live.sig.json" "$EXPECTED_PUB" "$LEGACY_CHANNEL" "$LIVE_PAYLOAD" >/dev/null 2>&1; then
                LIVE_KIND="legacy"
                legacy_payload_matches "$LIVE_PAYLOAD" || exit 1
            else
                echo "✖ the LIVE envelope does not authenticate against the committed key (neither as $CHANNEL nor as the compiled legacy $LEGACY_CHANNEL descriptor) — hard stop (never treated as absent)"; exit 1
            fi
            LIVE_SEQ="$(python3 -c "import json;print(json.load(open('$LIVE_PAYLOAD'))['sequence'])")"
            LIVE_VER="$(python3 -c "import json;print(json.load(open('$LIVE_PAYLOAD'))['version'])")"
            [[ "$LIVE_SEQ" =~ ^[1-9][0-9]*$ ]] || { echo "✖ live sequence '$LIVE_SEQ' is not a positive integer"; exit 1; }
            ;;
        404)
            # Fail closed forever: the signed app channel was bootstrapped
            # once (v0.7.4) — a missing live envelope means an outage or a
            # deleted release, and publishing waits; it never restarts from
            # nothing (there is no --bootstrap any more).
            echo "✖ no live signed release reachable at $LIVE_URL — refusing (bootstrap retired after v0.7.4; publishing never restarts from nothing)"; exit 1;;
        *)  echo "✖ cannot read the live envelope (HTTP $LIVE_STATUS) — refusing to guess"; exit 1;;
    esac
}

# Supersession gate for a candidate SEQUENCE (called twice: before the
# build, and again right before the release is created).
check_supersession() {
    local when="$1"
    read_live
    if [ "$LIVE_KIND" = "legacy" ]; then
        echo "  live ($when): v$LIVE_VER sequence $LIVE_SEQ (LEGACY $LEGACY_CHANNEL envelope — pre-rename channel state)"
        # Only the transition release may stand on the legacy floor: exactly
        # the next sequence. Anything later must supersede a $CHANNEL envelope.
        [ "$SEQUENCE" -eq $((LEGACY_SEQUENCE + 1)) ] || {
            echo "✖ ($when) only the transition release (sequence $((LEGACY_SEQUENCE + 1))) may supersede the legacy $LEGACY_CHANNEL envelope — source sequence is $SEQUENCE; a $CHANNEL release must be live first"; exit 1; }
    else
        echo "  live ($when): v$LIVE_VER sequence $LIVE_SEQ"
    fi
    [ "$SEQUENCE" -gt "$LIVE_SEQ" ] || {
        echo "✖ superseded ($when): sequence $SEQUENCE is not greater than live $LIVE_SEQ — bump APP_RELEASE_SEQUENCE"; exit 1; }
}

# --- 1. source state
if [ "$ALLOW_DIRTY" = 0 ] && [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    echo "✖ working tree is not clean — commit first (or --allow-dirty for a dry run)"; exit 1
fi
VERSION="$(python3 -c "import json; print(json.load(open('manifest.json'))['version'])")"
if [ -n "$VERSION_ARG" ] && [ "${VERSION_ARG#v}" != "$VERSION" ]; then
    echo "✖ requested version ${VERSION_ARG#v} != manifest.json version $VERSION"; exit 1
fi
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "✖ '$VERSION' is not exact SemVer"; exit 1; }
# The tag is pinned to THIS reviewed commit, which must already be on
# origin/main (GitHub cannot tag a commit it does not have, and a tag on a
# commit nobody reviewed on main is exactly what we refuse to create).
HEAD_SHA="$(git rev-parse HEAD 2>/dev/null)" || { echo "✖ not a git checkout"; exit 1; }
git remote get-url origin >/dev/null 2>&1 || { echo "✖ no origin remote — cannot prove HEAD is on origin/main"; exit 1; }
git fetch -q origin main || { echo "✖ git fetch origin main failed"; exit 1; }
git merge-base --is-ancestor "$HEAD_SHA" origin/main || {
    echo "✖ HEAD $HEAD_SHA is not on origin/main — push (and review) first"; exit 1; }
SEQUENCE="$(grep -E '^APP_RELEASE_SEQUENCE = [0-9]+$' py/release_verify.py | grep -oE '[0-9]+')"
[[ "$SEQUENCE" =~ ^[1-9][0-9]*$ ]] || { echo "✖ APP_RELEASE_SEQUENCE not found in py/release_verify.py"; exit 1; }
grep -q "\"$KEYID\"" py/release_verify.py || {
    echo "✖ py/release_verify.py does not pin $KEYID — the app would refuse its own update"; exit 1; }
TAG="v$VERSION"
echo "release $TAG (sequence $SEQUENCE, key $KEYID, commit ${HEAD_SHA:0:12})"

# --- 2. deterministic build
python3 scripts/build_click.py >/dev/null
# The click filename is DERIVED from manifest.json exactly like build_click.py
# derives it (package name + version) — a hardcoded name here and a derived
# one there is how a rename silently publishes nothing (plan §5).
PKG_NAME="$(python3 -c "import json;print(json.load(open('manifest.json'))['name'])")"
CLICK="build/${PKG_NAME}_${VERSION}_all.click"
[ -f "$CLICK" ] || { echo "✖ build did not produce $CLICK"; exit 1; }
FILENAME="$(basename "$CLICK")"
SHA256="$("$OPENSSL" dgst -sha256 -hex < "$CLICK" | awk '{print $NF}')"
SIZE="$(wc -c < "$CLICK" | tr -d ' ')"
echo "  built $FILENAME ($SIZE bytes, sha256 $SHA256)"

# --- 3. supersession against the AUTHENTICATED live channel
check_supersession "before build"

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
    "channel": "briglia-ut",
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
policy = rv.ReleasePolicy("briglia-ut", {key_id: pub_hex},
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
# Second authenticated supersession check, immediately before anything is
# created: a concurrent or interleaved publisher that got ahead of us since
# the first check must stop us here, never be "un-latested" by us.
check_supersession "before publish"
RELEASE_ID_OUT="$WORK/release-id" \
ASSETS="$DIST/$FILENAME
$DIST/manifest.json
$DIST/manifest.sig.json" \
REPO="$REPO" REF_NAME="$TAG" VERSION="$VERSION" TITLE="$TITLE" TARGET_COMMITISH="$HEAD_SHA" \
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
# Independent tag binding check before anything is recorded: the published
# tag, resolved through the refs API (never the release's target_commitish
# echo), must name the reviewed HEAD.
TAG_COMMIT="$(GH_API_URL="$API" "$RELEASE_SCRIPTS/resolve-tag-commit.sh" "$REPO" "$TAG")" || {
    echo "✖ cannot resolve refs/tags/$TAG after publication — NOT recorded; investigate"; exit 1; }
[ "$TAG_COMMIT" = "$HEAD_SHA" ] || {
    echo "✖ refs/tags/$TAG names commit $TAG_COMMIT, not the reviewed HEAD $HEAD_SHA — the published release is bound to the wrong commit; NOT recorded; investigate before anything else"; exit 1; }
echo "✔ public state verified: $TAG immutable, tag → ${HEAD_SHA:0:12}, envelope + click byte-identical"

# --- 7. record (outside the repository) only after verification
mkdir -p "$(dirname "$LOG")"
python3 - "$LOG" "$TAG" "$VERSION" "$SEQUENCE" "$SHA256" "$ENVELOPE_SHA" "$RELEASE_ID" "$KEYID" "$HEAD_SHA" <<'PYEOF'
import datetime, json, sys
log, tag, version, seq, sha, env_sha, rid, key, commit = sys.argv[1:10]
with open(log, "a") as f:
    f.write(json.dumps({"tag": tag, "version": version, "sequence": int(seq), "clickSha256": sha,
                        "envelopeSha256": env_sha, "releaseId": int(rid), "keyId": key, "commit": commit,
                        "recorded": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}) + "\n")
PYEOF
echo "  recorded in $LOG"

echo "✔ $TAG is live: $DOWNLOAD_BASE/v$VERSION/$FILENAME"
