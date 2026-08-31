#!/usr/bin/env python3
"""Deterministic release-channel watcher (ada-cli RELEASE_SIGNING_PLAN.md §10).

Re-verifies the two signed channels — Ada CLI and this app — the way a
client would, then cross-checks what GitHub says about them, and alerts a
human over Telegram on ANY mismatch or inability to verify. Success is
silent. No LLM is involved: every judgement here is a byte, hash, number
or string comparison against pinned keys and a locally recorded history.

    release_watch.py check      [--config PATH]   # hourly
    release_watch.py heartbeat  [--config PATH]   # every few hours: alerts if
                                                  # `check` has stopped completing
    release_watch.py status     [--config PATH]   # print the recorded state

Per channel, `check`:
  1. fetches `releases/latest/download/manifest.sig.json` (bounded) and
     authenticates it with the PINNED key set (py/release_verify.py — the
     same code the phone runs: signature, schema, channel, expiry, asset
     URLs locked to the per-version release location);
  2. compares it with the newest RECORDED authorized release: identical →
     fine; strictly higher sequence → a candidate new release that must be
     corroborated (CLI: the "Release (signed)" workflow run for that exact
     tag commit succeeded, every job; app: the local publication log written
     by publish_click.sh names exactly this tag/sequence/envelope) before it
     is recorded — and its recording is announced, never silent; lower
     sequence → ROLLBACK alert; same sequence but any difference → alert;
  3. asks the GitHub API: the latest release must be non-draft, immutable,
     carry the manifest's tag, and refs/tags/<tag> must still resolve to the
     recorded commit; no non-draft release may carry a higher version than
     `latest` (a confused/frozen latest pointer);
  4. every asset in the manifest must be reachable at its immutable URL
     with the authenticated size (Range probe, hourly); once a day, or
     whenever the recorded release changes, every asset is downloaded in
     full with the size bound and its SHA-256 compared;
  5. CLI only: the released install.sh must be byte-identical to
     scripts/get-ada.sh at the exact release tag;
  6. optional website checks: the CLI install command must resolve (via
     redirect) to the released installer bytes; the app page must link the
     exact click URL from the app manifest;
  7. optional transition check: a legacy Blob manifest still in service must
     agree with the authoritative release;
  8. metadata expiry within the warning window is an alert.

Alert policy: a finding is sent when it first appears and re-sent every
`realert_hours` while it persists; when it clears, one recovery message is
sent. Undelivered Telegram messages are queued in the state file and
retried on the next run. `heartbeat` alerts when the last completed `check`
is older than `heartbeat_max_age_hours` (the watcher watching itself).

Config (JSON; the installer writes the production one): see DEFAULT_CONFIG.
State: <state_dir>/state.json under an exclusive lock — the recorded
authorized releases, alert bookkeeping, timestamps. Stdlib only.
"""

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
# Checkout layout: scripts/ next to py/. Installed snapshot layout: py/ inside
# the watcher's own directory (scripts/install_release_watch.sh).
for _cand in (os.path.join(HERE, "py"), os.path.join(os.path.dirname(HERE), "py")):
    if os.path.isfile(os.path.join(_cand, "release_verify.py")):
        sys.path.insert(0, _cand)
        break
import release_verify as rv  # noqa: E402

WATCH_VERSION = "1"
USER_AGENT = "ada-release-watch/" + WATCH_VERSION
MAX_SMALL_FETCH = 512 * 1024          # envelopes, installers, API JSON, pages
MAX_PAGE_FETCH = 4 * 1024 * 1024
FULL_HASH_INTERVAL = 24 * 3600

DEFAULT_CONFIG = {
    "state_dir": "~/.config/ada-release-watch",
    "github_api": "https://api.github.com",
    "raw_base": "https://raw.githubusercontent.com",
    "telegram_env_file": "~/.claude/channels/telegram/.env",   # TELEGRAM_BOT_TOKEN, OWNER_CHAT_ID
    "telegram_api": "https://api.telegram.org",
    "realert_hours": 6,
    "heartbeat_max_age_hours": 3,
    "expiry_warning_days": 30,
    "channels": {
        "ada-cli": {
            "repo": "permaevidence/ada-cli",
            "workflow_name": "Release (signed)",
            "installer_asset": "install.sh",
            "installer_source": "scripts/get-ada.sh",
            "website_install_url": "https://ada-app-psi.vercel.app/cli/install.sh",
            "legacy_blob_manifest": None,
        },
        "ada-ut": {
            "repo": "permaevidence/ada-ut",
            "publication_log": "~/.ada-release-keys/ada-ut-publications.jsonl",
            "website_page_url": "https://ada-app-psi.vercel.app/app",
            "legacy_blob_manifest": None,
        },
    },
}

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


# ------------------------------------------------------------------ utils

def now_ts():
    return time.time()


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch(url, max_bytes=MAX_SMALL_FETCH, headers=None, timeout=60, method="GET"):
    """Bounded fetch → (status, headers, bytes). Network errors raise."""
    h = {"User-Agent": USER_AGENT}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise WatchError("response from %s exceeds %d bytes" % (url, max_bytes))
            return resp.status, dict(resp.headers), data
    except urllib.error.HTTPError as exc:
        body = exc.read(max_bytes + 1) if exc.fp else b""
        return exc.code, dict(exc.headers or {}), body[:max_bytes]


class WatchError(Exception):
    pass


def gh_json(cfg, path, params=None):
    url = cfg["github_api"] + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    status, _, body = fetch(url, headers=headers)
    if status != 200:
        raise WatchError("GitHub API %s → HTTP %s" % (path, status))
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        raise WatchError("GitHub API %s → invalid JSON" % path)


def semver_tuple(version):
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version or "")
    return tuple(int(x) for x in m.groups()) if m else None


# ------------------------------------------------------------------ state

class State:
    def __init__(self, state_dir):
        self.dir = os.path.expanduser(state_dir)
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        self.path = os.path.join(self.dir, "state.json")
        self.lock_path = os.path.join(self.dir, "state.lock")
        self.lock_fd = None
        self.data = None

    def __enter__(self):
        self.lock_fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock_fd)
            self.lock_fd = None
            raise WatchError("another release_watch run holds %s" % self.lock_path)
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.data = json.load(f)   # a corrupt state file is a hard error, on purpose
        else:
            self.data = {}
        self.data.setdefault("version", WATCH_VERSION)
        self.data.setdefault("recorded", {})
        self.data.setdefault("full_hash_at", {})
        self.data.setdefault("active", {})       # finding key → {"first": ts, "last_sent": ts, "text": ...}
        self.data.setdefault("queued", [])       # undelivered messages
        self.data.setdefault("announced", {})    # channel → last announced sequence
        return self

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def __exit__(self, *exc):
        if self.lock_fd is not None:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            os.close(self.lock_fd)


# --------------------------------------------------------------- telegram

def telegram_credentials(cfg):
    path = os.path.expanduser(cfg["telegram_env_file"])
    token = chat = None
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
            elif line.startswith("OWNER_CHAT_ID="):
                chat = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not token or not chat:
        raise WatchError("telegram env file %s lacks TELEGRAM_BOT_TOKEN / OWNER_CHAT_ID" % path)
    return token, chat


def send_telegram(cfg, text):
    """True when Telegram confirmed delivery. Never raises."""
    try:
        token, chat = telegram_credentials(cfg)
        body = json.dumps({"chat_id": chat, "text": text[:4000],
                           "disable_web_page_preview": True}).encode()
        status, _, resp = _post(cfg["telegram_api"] + "/bot" + token + "/sendMessage", body)
        return status == 200 and json.loads(resp.decode("utf-8", "replace")).get("ok") is True
    except Exception as exc:  # noqa: BLE001 — delivery problems are reported, not fatal
        print("  ! telegram delivery failed: %s" % _scrub(str(exc), cfg), file=sys.stderr)
        return False


def _post(url, body):
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read(MAX_SMALL_FETCH)
    except urllib.error.HTTPError as exc:
        return exc.code, {}, exc.read(MAX_SMALL_FETCH) if exc.fp else b""


def _scrub(text, cfg):
    """Never let a bot token reach logs/state through an error string."""
    try:
        token, _ = telegram_credentials(cfg)
        return text.replace(token, "[TOKEN]")
    except Exception:  # noqa: BLE001
        return text


# ------------------------------------------------------------ findings

class Run:
    """Collects findings for one `check` run and turns them into messages."""

    def __init__(self, cfg, state):
        self.cfg = cfg
        self.state = state
        self.findings = {}   # key → text (ALERT level)
        self.infos = []      # one-off informational messages

    def alert(self, key, text):
        print("  ✖ %s: %s" % (key, text))
        self.findings[key] = text

    def info(self, text):
        print("  ℹ %s" % text)
        self.infos.append(text)

    def ok(self, text):
        print("  ✔ %s" % text)

    def flush(self, now):
        """Decide what to send, send it, update bookkeeping. Returns the
        list of messages actually composed (sent or queued)."""
        cfg, st = self.cfg, self.state.data
        realert = float(cfg["realert_hours"]) * 3600
        messages = []
        active = st["active"]
        for key, text in self.findings.items():
            prev = active.get(key)
            if prev is None:
                active[key] = {"first": now, "last_sent": now, "text": text}
                messages.append("🚨 ada release watch — %s\n%s" % (key, text))
            elif now - prev["last_sent"] >= realert or prev.get("text") != text:
                prev["last_sent"] = now
                prev["text"] = text
                messages.append("🚨 ada release watch — STILL FAILING since %s — %s\n%s"
                                % (iso(prev["first"]), key, text))
        for key in list(active):
            if key.startswith("watch/"):
                continue   # owned by `heartbeat`, which reconciles it itself
            if key not in self.findings:
                first = active.pop(key)["first"]
                messages.append("✅ ada release watch — recovered: %s (failing since %s)" % (key, iso(first)))
        for text in self.infos:
            messages.append("ℹ️ ada release watch — %s" % text)
        # deliver queued first (oldest), then new; keep whatever fails
        pending = list(st["queued"]) + messages
        st["queued"] = []
        for m in pending:
            if not send_telegram(cfg, m):
                st["queued"].append(m)
        return messages


# ---------------------------------------------------------- channel check

def policy_for(cfg, channel):
    base = rv.CLI_POLICY if channel == "ada-cli" else rv.APP_POLICY
    chan = cfg["channels"][channel]
    # Test/staging overrides only through the config file — never the environment.
    if chan.get("envelope_url") or chan.get("artifact_url_prefix"):
        return rv.ReleasePolicy(base.channel, {k: v.hex() for k, v in base.keys.items()},
                                chan.get("envelope_url", base.envelope_url),
                                chan.get("artifact_url_prefix", base.artifact_url_prefix),
                                base.min_sequence)
    return base


def manifest_record(manifest, envelope_raw, tag_commit):
    return {
        "tag": "v" + manifest["version"],
        "version": manifest["version"],
        "sequence": manifest["sequence"],
        "expires": iso(manifest["expires"]),      # the verifier hands back epoch floats
        "published": iso(manifest["published"]),
        "envelope_sha256": hashlib.sha256(envelope_raw).hexdigest(),
        "assets": {k: {"url": v["url"], "sha256": v["sha256"], "size": v["size"]}
                   for k, v in manifest["platforms"].items()},
        "commit": tag_commit,
    }


def resolve_tag(cfg, repo, tag):
    """Commit a tag names, following annotated tags; None if the tag is absent."""
    try:
        ref = gh_json(cfg, "/repos/%s/git/ref/tags/%s" % (repo, tag))
    except WatchError as exc:
        if "HTTP 404" in str(exc):
            return None
        raise
    if ref.get("ref") != "refs/tags/" + tag:
        raise WatchError("ref lookup answered %r for %s" % (ref.get("ref"), tag))
    obj = ref.get("object") or {}
    for _ in range(6):
        sha, typ = str(obj.get("sha", "")), obj.get("type")
        if not _SHA_RE.match(sha):
            raise WatchError("malformed ref object for %s" % tag)
        if typ == "commit":
            return sha
        if typ != "tag":
            raise WatchError("unexpected ref object type %r for %s" % (typ, tag))
        obj = (gh_json(cfg, "/repos/%s/git/tags/%s" % (repo, sha)).get("object")) or {}
    raise WatchError("annotated tag chain too deep for %s" % tag)


def probe_asset(url, size):
    """Range probe: the immutable URL must answer with exactly `size` total bytes."""
    status, headers, body = fetch(url, max_bytes=2, headers={"Range": "bytes=0-0"})
    if status == 206:
        cr = headers.get("Content-Range") or headers.get("content-range") or ""
        m = re.search(r"/(\d+)$", cr)
        if not m:
            return "no Content-Range in 206 response"
        if int(m.group(1)) != size:
            return "server reports %s bytes, signed size is %d" % (m.group(1), size)
        return None
    if status == 200:
        cl = headers.get("Content-Length") or headers.get("content-length")
        if cl is not None and int(cl) != size:
            return "server reports %s bytes, signed size is %d" % (cl, size)
        return None
    return "HTTP %s" % status


def full_hash(url, size, sha256):
    with tempfile.NamedTemporaryFile(prefix="ada-watch-", delete=True) as tmp:
        err = rv.download_to_file(url, tmp.name, size, sha256, timeout=600)
    return err


def corroborate_cli(cfg, chan, run, record):
    """The signed workflow run for this exact tag commit succeeded, every job."""
    repo = chan["repo"]
    runs = gh_json(cfg, "/repos/%s/actions/runs" % repo,
                   {"event": "push", "branch": record["tag"], "per_page": 20}).get("workflow_runs", [])
    match = [r for r in runs if r.get("name") == chan["workflow_name"]
             and r.get("head_sha") == record["commit"]]
    if not match:
        return "no '%s' workflow run found for %s at %s" % (chan["workflow_name"], record["tag"], record["commit"][:12])
    r = sorted(match, key=lambda r: r.get("run_number", 0))[-1]
    if r.get("status") != "completed" or r.get("conclusion") != "success":
        return "workflow run %s for %s is %s/%s" % (r.get("id"), record["tag"], r.get("status"), r.get("conclusion"))
    jobs = gh_json(cfg, "/repos/%s/actions/runs/%s/jobs" % (repo, r["id"]), {"per_page": 100}).get("jobs", [])
    bad = [j["name"] for j in jobs if j.get("conclusion") not in ("success", "skipped")]
    if not jobs or bad:
        return "workflow run %s has failed/unknown jobs: %s" % (r.get("id"), bad or "none listed")
    verify_jobs = [j for j in jobs if j["name"].startswith("Verify public channel")]
    if len(verify_jobs) < 3 or any(j.get("conclusion") != "success" for j in verify_jobs):
        return "workflow run %s lacks three successful public verification jobs" % r.get("id")
    record["workflow_run"] = r["id"]
    return None


def corroborate_app(cfg, chan, run, record):
    """The local publisher recorded exactly this release (publish_click.sh
    writes the log only after its own public re-verification)."""
    path = os.path.expanduser(chan.get("publication_log") or "")
    if not path or not os.path.exists(path):
        return "no local publication log at %s — cannot corroborate a new app release" % (path or "<unset>")
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    hits = [e for e in entries if e.get("tag") == record["tag"]]
    if not hits:
        return "publication log has no entry for %s" % record["tag"]
    e = hits[-1]
    problems = []
    if e.get("sequence") != record["sequence"]:
        problems.append("sequence %s≠%s" % (e.get("sequence"), record["sequence"]))
    if e.get("envelopeSha256") != record["envelope_sha256"]:
        problems.append("envelope sha256 differs")
    if e.get("commit") != record["commit"]:
        problems.append("commit %s≠%s" % (str(e.get("commit"))[:12], record["commit"][:12]))
    click = record["assets"].get("click") or {}
    if e.get("clickSha256") != click.get("sha256"):
        problems.append("click sha256 differs")
    return ("publication log disagrees with the live release: " + ", ".join(problems)) if problems else None


def check_channel(cfg, channel, run, now):
    chan = cfg["channels"][channel]
    repo = chan["repo"]
    st = run.state.data
    policy = policy_for(cfg, channel)
    print("— %s (%s) —" % (channel, repo))

    # 1. authenticate the live envelope
    try:
        raw = rv.bounded_fetch(policy.envelope_url, rv.MAX_ENVELOPE_BYTES)
    except Exception as exc:  # noqa: BLE001
        run.alert(channel + "/envelope-unreachable", "cannot fetch %s: %s" % (policy.envelope_url, exc))
        return
    try:
        manifest = rv.verify_envelope(raw, policy, now)
    except rv.ReleaseVerifyError as exc:
        run.alert(channel + "/envelope-invalid", "live envelope REJECTED (%s): %s" % (exc.kind, exc))
        return
    run.ok("live envelope authenticates: v%s sequence %d" % (manifest["version"], manifest["sequence"]))
    tag = "v" + manifest["version"]
    rec = st["recorded"].get(channel)
    # Rollback is judged BEFORE anything GitHub-dependent: a replayed older
    # envelope must trip this even when the API is unreachable or confused.
    if rec is not None and rec["sequence"] > manifest["sequence"]:
        run.alert(channel + "/rollback",
                  "latest serves %s (sequence %d) but %s (sequence %d) was recorded — rollback, replay or deleted release"
                  % (tag, manifest["sequence"], rec["tag"], rec["sequence"]))

    # 3a. GitHub: latest release + tag → commit
    try:
        latest = gh_json(cfg, "/repos/%s/releases/latest" % repo)
        tag_commit = resolve_tag(cfg, repo, tag)
    except WatchError as exc:
        run.alert(channel + "/github-unreachable", "cannot query GitHub: %s" % exc)
        return
    if latest.get("tag_name") != tag or latest.get("draft") is not False:
        run.alert(channel + "/latest-mismatch",
                  "GitHub 'latest' is %s (draft=%s) but the envelope served as latest is %s"
                  % (latest.get("tag_name"), latest.get("draft"), tag))
    if latest.get("immutable") is not True:
        run.alert(channel + "/not-immutable", "release %s is not immutable" % tag)
    if not tag_commit:
        run.alert(channel + "/tag-missing", "refs/tags/%s does not exist" % tag)
        return
    live = manifest_record(manifest, raw, tag_commit)

    # 2. compare with the recorded authorized release
    if rec is None or rec["sequence"] < live["sequence"]:
        why = (corroborate_cli if channel == "ada-cli" else corroborate_app)(cfg, chan, run, live)
        if why:
            run.alert(channel + "/uncorroborated-release",
                      "%s (sequence %d) is live but NOT corroborated: %s — not recorded"
                      % (tag, live["sequence"], why))
            # keep checking the bytes of what is live anyway
        else:
            st["recorded"][channel] = live
            st["full_hash_at"].pop(channel, None)   # force a full hash below
            run.info("%s: %s (sequence %d, commit %s) corroborated and RECORDED as the authorized release"
                     % (channel, tag, live["sequence"], tag_commit[:12]))
            rec = live
    elif rec["sequence"] > live["sequence"]:
        pass   # already reported above
    else:
        diffs = [k for k in ("tag", "version", "expires", "published", "envelope_sha256", "assets", "commit")
                 if rec.get(k) != live.get(k)]
        if diffs:
            run.alert(channel + "/record-mismatch",
                      "live %s differs from the recorded release with the same sequence %d: %s"
                      % (tag, live["sequence"], ", ".join(diffs)))
        else:
            run.ok("matches the recorded authorized release (%s, commit %s)" % (rec["tag"], rec["commit"][:12]))

    # 3b. latest pointer confusion: no non-draft release may out-version latest
    try:
        releases = gh_json(cfg, "/repos/%s/releases" % repo, {"per_page": 100})
    except WatchError as exc:
        run.alert(channel + "/github-unreachable", "cannot list releases: %s" % exc)
        releases = []
    newer = [r["tag_name"] for r in releases
             if not r.get("draft") and semver_tuple(r.get("tag_name"))
             and semver_tuple(r["tag_name"]) > semver_tuple(tag)]
    if newer:
        run.alert(channel + "/latest-frozen",
                  "non-draft release(s) newer than latest %s exist: %s" % (tag, ", ".join(sorted(newer))))
    drafts = [r["tag_name"] for r in releases if r.get("draft")]
    if drafts:
        run.info("%s: draft release(s) present: %s" % (channel, ", ".join(drafts)))

    # 4. assets: probe hourly, full hash daily / after change
    problems = []
    for name, a in live["assets"].items():
        err = None
        try:
            err = probe_asset(a["url"], a["size"])
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
        if err:
            problems.append("%s: %s" % (name, err))
    if problems:
        run.alert(channel + "/asset-unreachable", "; ".join(problems))
    else:
        run.ok("%d asset(s) reachable with the signed sizes" % len(live["assets"]))
    last_full = st["full_hash_at"].get(channel, 0)
    if not problems and (now - last_full >= FULL_HASH_INTERVAL):
        bad = []
        for name, a in live["assets"].items():
            try:
                err = full_hash(a["url"], a["size"], a["sha256"])
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
            if err:
                bad.append("%s: %s" % (name, err))
        if bad:
            run.alert(channel + "/asset-hash", "full download does not match the signed hash/size: " + "; ".join(bad))
        else:
            st["full_hash_at"][channel] = now
            run.ok("full download of every asset matches the signed sha256 + size")

    # 5. installer byte-compare (CLI)
    if chan.get("installer_asset"):
        rel_url = policy.artifact_url_prefix.format(version=manifest["version"]) + chan["installer_asset"]
        src_url = "%s/%s/%s/%s" % (cfg["raw_base"], repo, tag, chan["installer_source"])
        try:
            s1, _, released = fetch(rel_url)
            s2, _, source = fetch(src_url)
            if s1 != 200 or s2 != 200:
                run.alert(channel + "/installer", "installer fetch: release HTTP %s, source HTTP %s" % (s1, s2))
            elif released != source:
                run.alert(channel + "/installer",
                          "released %s differs from %s at %s" % (chan["installer_asset"], chan["installer_source"], tag))
            else:
                run.ok("released installer is byte-identical to %s@%s" % (chan["installer_source"], tag))
                if chan.get("website_install_url"):
                    s3, _, via_site = fetch(chan["website_install_url"])
                    if s3 != 200 or via_site != released:
                        run.alert(channel + "/website-installer",
                                  "%s does not resolve to the released installer (HTTP %s)" % (chan["website_install_url"], s3))
                    else:
                        run.ok("website install URL resolves to the released installer")
        except Exception as exc:  # noqa: BLE001
            run.alert(channel + "/installer", "installer check failed: %s" % exc)

    # 6. website page must link the exact asset (app)
    if chan.get("website_page_url"):
        try:
            s, _, page = fetch(chan["website_page_url"], max_bytes=MAX_PAGE_FETCH)
            urls = [a["url"] for a in live["assets"].values()]
            if s != 200:
                run.alert(channel + "/website-page", "%s → HTTP %s" % (chan["website_page_url"], s))
            elif not all(u.encode() in page for u in urls):
                run.alert(channel + "/website-page", "%s does not link the released asset(s) %s"
                          % (chan["website_page_url"], ", ".join(urls)))
            else:
                run.ok("website page links the released asset")
        except Exception as exc:  # noqa: BLE001
            run.alert(channel + "/website-page", "page check failed: %s" % exc)

    # 7. transition: a legacy manifest still in service must agree
    if chan.get("legacy_blob_manifest"):
        try:
            s, _, body = fetch(chan["legacy_blob_manifest"])
            legacy = json.loads(body.decode("utf-8")) if s == 200 else None
            click = live["assets"].get("click") or next(iter(live["assets"].values()))
            if not legacy or legacy.get("version") != manifest["version"] or legacy.get("sha256") != click["sha256"]:
                run.alert(channel + "/legacy-blob",
                          "legacy manifest %s (HTTP %s, version %s) disagrees with the authoritative %s"
                          % (chan["legacy_blob_manifest"], s, (legacy or {}).get("version"), tag))
            else:
                run.ok("legacy transition manifest agrees with the authoritative release")
        except Exception as exc:  # noqa: BLE001
            run.alert(channel + "/legacy-blob", "legacy manifest check failed: %s" % exc)

    # 8. expiry
    days_left = (manifest["expires"] - now) / 86400
    if days_left < float(cfg["expiry_warning_days"]):
        run.alert(channel + "/expiry", "metadata for %s expires in %.1f days (%s) — publish a new release before clients refuse it"
                  % (tag, days_left, iso(manifest["expires"])))
    else:
        run.ok("metadata valid for another %.0f days" % days_left)


# ------------------------------------------------------------- commands

def load_config(path):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path:
        with open(os.path.expanduser(path)) as f:
            user = json.load(f)
        for k, v in user.items():
            if k == "channels":
                for ch, chv in v.items():
                    cfg["channels"].setdefault(ch, {}).update(chv)
            else:
                cfg[k] = v
    return cfg


def cmd_check(cfg):
    now = now_ts()
    with State(cfg["state_dir"]) as state:
        run = Run(cfg, state)
        for channel in cfg["channels"]:
            try:
                check_channel(cfg, channel, run, now)
            except Exception as exc:  # noqa: BLE001 — a crash in one channel must still alert
                run.alert(channel + "/watcher-error", "watcher raised %s: %s" % (type(exc).__name__, exc))
        messages = run.flush(now)
        state.data["last_run"] = now
        state.data["last_completed"] = now
        if not run.findings:
            state.data["last_clean"] = now
        state.save()
    print("check complete: %d finding(s), %d message(s), %d queued"
          % (len(run.findings), len(messages), len(state.data["queued"])))
    return 2 if run.findings else 0


def cmd_heartbeat(cfg):
    now = now_ts()
    max_age = float(cfg["heartbeat_max_age_hours"]) * 3600
    with State(cfg["state_dir"]) as state:
        st = state.data
        last = st.get("last_completed")
        stale = last is None or now - last > max_age
        msg = None
        if stale:
            key = "watch/heartbeat"
            prev = st["active"].get(key)
            realert = float(cfg["realert_hours"]) * 3600
            if prev is None or now - prev["last_sent"] >= realert:
                st["active"][key] = {"first": (prev or {}).get("first", now), "last_sent": now, "text": "stale"}
                msg = ("🚨 ada release watch — the hourly check has not completed since %s (limit %.0fh). "
                       "The watcher itself may be broken or the Mac may be offline."
                       % (iso(last) if last else "never", max_age / 3600))
        else:
            if st["active"].pop("watch/heartbeat", None):
                msg = "✅ ada release watch — hourly checks are completing again (last %s)" % iso(last)
        pending = list(st["queued"]) + ([msg] if msg else [])
        st["queued"] = []
        for m in pending:
            if not send_telegram(cfg, m):
                st["queued"].append(m)
        st["last_heartbeat"] = now
        state.save()
    print("heartbeat: %s (last completed check: %s)" % ("STALE" if stale else "ok", iso(last) if last else "never"))
    return 2 if stale else 0


def cmd_status(cfg):
    with State(cfg["state_dir"]) as state:
        print(json.dumps(state.data, indent=2, sort_keys=True))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["check", "heartbeat", "status"])
    ap.add_argument("--config", help="JSON config overriding DEFAULT_CONFIG")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    print("ada release watch v%s — %s — %s" % (WATCH_VERSION, args.command, iso(now_ts())))
    try:
        return {"check": cmd_check, "heartbeat": cmd_heartbeat, "status": cmd_status}[args.command](cfg)
    except WatchError as exc:
        print("✖ %s" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
