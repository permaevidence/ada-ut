#!/usr/bin/env python3
"""Battery for scripts/release_watch.py against an in-process fake of
everything it talks to — GitHub Releases/refs/Actions API, the release
download host (with Range), raw.githubusercontent, the website, a legacy
Blob manifest and the Telegram Bot API — with fault injection. Runs the
REAL watcher on a throwaway copy of this repository whose pinned keys are
generated test keys, so every alert path is exercised end to end.

    python3 scripts/watch_selftest.py
"""

import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from signing_fixture import TestKey  # noqa: E402

PASSED = FAILED = 0


def check(label, ok, detail=""):
    global PASSED, FAILED
    print("  %s %s%s" % ("✔" if ok else "✖", label, "" if ok or not detail else " — " + str(detail)[-500:]))
    if ok:
        PASSED += 1
    else:
        FAILED += 1


class Fake:
    """One server: envelopes, downloads, GitHub API, raw source, website,
    legacy manifest, Telegram. All state is plain dicts the test mutates."""

    def __init__(self):
        self.envelopes = {}        # channel -> bytes served as latest
        self.assets = {}           # (channel, version, name) -> bytes
        self.releases = {}         # repo -> [ {tag_name, draft, immutable, id} ]
        self.tags = {}             # repo -> {tag: sha}
        self.runs = {}             # repo -> [ {id, name, head_sha, head_branch, status, conclusion, jobs:[...]}, ]
        self.raw = {}              # (repo, tag, path) -> bytes
        self.site_installer = None  # bytes served by /site/cli/install.sh (None → redirect to release asset)
        self.site_installer_redirect = None
        self.site_page = b""
        self.legacy_manifest = None
        self.telegram = []         # captured message texts
        self.faults = {}           # range_total: int | None; asset_404: name; asset_sub: {name: bytes};
        #                          api_status: int; tg_status: int; ref_status: int
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body=b"", ctype="application/octet-stream", headers=None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if body and self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, status, obj):
                self._send(status, json.dumps(obj).encode(), "application/json")

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                m = re.match(r"^/tg/bot([^/]+)/sendMessage$", self.path)
                if m:
                    if fake.faults.get("tg_status"):
                        return self._json(fake.faults["tg_status"], {"ok": False})
                    if m.group(1) != "tok":
                        return self._json(401, {"ok": False})
                    fake.telegram.append(json.loads(body)["text"])
                    return self._json(200, {"ok": True, "result": {}})
                self._send(404)

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                p, q = u.path, urllib.parse.parse_qs(u.query)
                m = re.match(r"^/latest/([^/]+)/manifest\.sig\.json$", p)
                if m:
                    env = fake.envelopes.get(m.group(1))
                    return self._send(200, env, "application/json") if env else self._send(404)
                m = re.match(r"^/download/([^/]+)/v([^/]+)/([^/]+)$", p)
                if m:
                    chan, ver, name = m.groups()
                    if fake.faults.get("asset_404") == name:
                        return self._send(404)
                    data = fake.assets.get((chan, ver, name))
                    if data is None:
                        return self._send(404)
                    data = fake.faults.get("asset_sub", {}).get(name, data)
                    rng = self.headers.get("Range")
                    if rng:
                        total = fake.faults.get("range_total") or len(data)
                        return self._send(206, data[:1], headers={"Content-Range": "bytes 0-0/%d" % total})
                    return self._send(200, data)
                m = re.match(r"^/raw/([^/]+/[^/]+)/([^/]+)/(.+)$", p)
                if m:
                    data = fake.raw.get((m.group(1), m.group(2), m.group(3)))
                    return self._send(200, data) if data is not None else self._send(404)
                if p == "/site/cli/install.sh":
                    if fake.site_installer is not None:
                        return self._send(200, fake.site_installer)
                    if fake.site_installer_redirect:
                        return self._send(308, headers={"Location": fake.site_installer_redirect})
                    return self._send(404)
                if p == "/site/app":
                    return self._send(200, fake.site_page, "text/html")
                if p == "/legacy/manifest.json":
                    if fake.legacy_manifest is None:
                        return self._send(404)
                    return self._send(200, json.dumps(fake.legacy_manifest).encode(), "application/json")
                if p.startswith("/api/"):
                    if fake.faults.get("api_status"):
                        return self._json(fake.faults["api_status"], {"message": "injected"})
                    m = re.match(r"^/api/repos/([^/]+/[^/]+)/releases/latest$", p)
                    if m:
                        rels = [r for r in fake.releases.get(m.group(1), []) if not r["draft"]]
                        return self._json(200, rels[-1]) if rels else self._json(404, {})
                    m = re.match(r"^/api/repos/([^/]+/[^/]+)/releases$", p)
                    if m:
                        return self._json(200, list(reversed(fake.releases.get(m.group(1), []))))
                    m = re.match(r"^/api/repos/([^/]+/[^/]+)/git/ref/tags/([^/]+)$", p)
                    if m:
                        if fake.faults.get("ref_status"):
                            return self._json(fake.faults["ref_status"], {})
                        sha = fake.tags.get(m.group(1), {}).get(m.group(2))
                        if not sha:
                            return self._json(404, {"message": "Not Found"})
                        if isinstance(sha, dict):   # annotated: {"tag_sha":..., "commit":...}
                            return self._json(200, {"ref": "refs/tags/" + m.group(2),
                                                    "object": {"type": "tag", "sha": sha["tag_sha"]}})
                        return self._json(200, {"ref": "refs/tags/" + m.group(2), "object": {"type": "commit", "sha": sha}})
                    m = re.match(r"^/api/repos/([^/]+/[^/]+)/git/tags/([0-9a-f]{40})$", p)
                    if m:
                        for t in fake.tags.get(m.group(1), {}).values():
                            if isinstance(t, dict) and t["tag_sha"] == m.group(2):
                                return self._json(200, {"object": {"type": "commit", "sha": t["commit"]}})
                        return self._json(404, {})
                    m = re.match(r"^/api/repos/([^/]+/[^/]+)/actions/runs$", p)
                    if m:
                        branch = q.get("branch", [None])[0]
                        runs = [dict(r, jobs=None) for r in fake.runs.get(m.group(1), []) if r["head_branch"] == branch]
                        return self._json(200, {"workflow_runs": runs})
                    m = re.match(r"^/api/repos/([^/]+/[^/]+)/actions/runs/(\d+)/jobs$", p)
                    if m:
                        r = next((r for r in fake.runs.get(m.group(1), []) if r["id"] == int(m.group(2))), None)
                        return self._json(200, {"jobs": r["jobs"]}) if r else self._json(404, {})
                self._send(404)

            do_HEAD = do_GET

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


JOBS_OK = [{"name": n, "conclusion": "success"} for n in
           ("Authorize (credential-free)", "Build macOS arm64", "Sign metadata", "Publish immutable release",
            "Verify public channel (linux)", "Verify public channel (macos)", "Verify public channel (linux-arm64)")]


def main():
    root = tempfile.mkdtemp(prefix="ada-watch-selftest-")
    repo_src = os.path.dirname(HERE)
    repo = os.path.join(root, "repo")
    os.makedirs(repo)
    for item in ("py", "scripts"):
        shutil.copytree(os.path.join(repo_src, item), os.path.join(repo, item),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    cli_key, app_key = TestKey("ada-cli"), TestKey("ada-ut")
    rv_path = os.path.join(repo, "py", "release_verify.py")
    s = open(rv_path).read()
    s = re.sub(r"# STAMP-CLI-KEY-BEGIN.*?# STAMP-CLI-KEY-END",
               '# STAMP-CLI-KEY-BEGIN\nCLI_KEYS = {\n    "%s":\n        "%s",\n}\n# STAMP-CLI-KEY-END' % (cli_key.key_id, cli_key.pub_hex), s, flags=re.S)
    s = re.sub(r"# STAMP-APP-KEY-BEGIN.*?# STAMP-APP-KEY-END",
               '# STAMP-APP-KEY-BEGIN\nAPP_KEYS = {\n    "%s":\n        "%s",\n}\n# STAMP-APP-KEY-END' % (app_key.key_id, app_key.pub_hex), s, flags=re.S)
    s = re.sub(r"^MIN_CLI_SEQUENCE = \d+$", "MIN_CLI_SEQUENCE = 1", s, flags=re.M)
    open(rv_path, "w").write(s)

    fake = Fake()
    B = fake.base
    tg_env = os.path.join(root, "tg.env")
    open(tg_env, "w").write("TELEGRAM_BOT_TOKEN=tok\nOWNER_CHAT_ID=1\n")
    pub_log = os.path.join(root, "ada-ut-publications.jsonl")
    state_dir = os.path.join(root, "state")
    cfg_path = os.path.join(root, "cfg.json")
    cfg = {
        "state_dir": state_dir, "github_api": B + "/api", "raw_base": B + "/raw",
        "telegram_env_file": tg_env, "telegram_api": B + "/tg",
        "realert_hours": 6, "heartbeat_max_age_hours": 3, "expiry_warning_days": 30,
        "channels": {
            "ada-cli": {"repo": "test/ada-cli", "workflow_name": "Release (signed)",
                        "installer_asset": "install.sh", "installer_source": "scripts/get-ada.sh",
                        "website_install_url": B + "/site/cli/install.sh",
                        "envelope_url": B + "/latest/ada-cli/manifest.sig.json",
                        "artifact_url_prefix": B + "/download/ada-cli/v{version}/",
                        "legacy_blob_manifest": None},
            "ada-ut": {"repo": "test/ada-ut", "publication_log": pub_log,
                       "website_page_url": B + "/site/app",
                       "envelope_url": B + "/latest/ada-ut/manifest.sig.json",
                       "artifact_url_prefix": B + "/download/ada-ut/v{version}/",
                       "legacy_blob_manifest": B + "/legacy/manifest.json"},
        },
    }
    json.dump(cfg, open(cfg_path, "w"))
    watcher = os.path.join(repo, "scripts", "release_watch.py")

    def run(cmd="check"):
        p = subprocess.run([sys.executable, watcher, cmd, "--config", cfg_path], capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr

    def state():
        return json.load(open(os.path.join(state_dir, "state.json")))

    def set_state(mut):
        st = state()
        mut(st)
        json.dump(st, open(os.path.join(state_dir, "state.json"), "w"))

    def sha(b):
        return hashlib.sha256(b).hexdigest()

    def envelope(key, chan, version, seq, assets, expires_days=180, published=None):
        payload = {"channel": chan, "expires": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + expires_days * 86400)),
                   "platforms": {n: {"sha256": sha(b), "size": len(b), "url": "%s/download/%s/v%s/%s" % (B, chan, version, n)}
                                 for n, b in assets.items()},
                   "published": published or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60)),
                   "schema": 1, "sequence": seq, "version": version}
        return key.sign(json.dumps(payload, sort_keys=True, indent=2).encode())

    def publish(chan, version, seq, assets, commit, expires_days=180, workflow="success", log=True, immutable=True):
        key = cli_key if chan == "ada-cli" else app_key
        repo_name = "test/" + chan
        for n, b in assets.items():
            fake.assets[(chan, version, n)] = b
        env = envelope(key, chan, version, seq, assets, expires_days)
        fake.envelopes[chan] = env
        rid = 1000 + len(fake.releases.get(repo_name, []))
        fake.releases.setdefault(repo_name, []).append({"id": rid, "tag_name": "v" + version, "draft": False, "immutable": immutable})
        fake.tags.setdefault(repo_name, {})["v" + version] = commit
        if chan == "ada-cli":
            fake.assets[(chan, version, "install.sh")] = assets.get("install.sh", b"")
            fake.raw[(repo_name, "v" + version, "scripts/get-ada.sh")] = assets.get("install.sh", b"")
            fake.site_installer_redirect = "%s/download/ada-cli/v%s/install.sh" % (B, version)
            if workflow:
                fake.runs.setdefault(repo_name, []).append({
                    "id": 500 + seq, "name": "Release (signed)", "head_sha": commit, "head_branch": "v" + version,
                    "run_number": seq, "status": "completed", "conclusion": workflow,
                    "jobs": JOBS_OK if workflow == "success" else [{"name": "Sign metadata", "conclusion": "failure"}]})
        else:
            click = assets["click"]
            fake.site_page = ("<a href=\"%s/download/ada-ut/v%s/click\">download</a>" % (B, version)).encode()
            fake.legacy_manifest = {"version": version, "sha256": sha(click), "size": len(click)}
            if log:
                with open(pub_log, "a") as f:
                    f.write(json.dumps({"tag": "v" + version, "version": version, "sequence": seq, "clickSha256": sha(click),
                                        "envelopeSha256": sha(env), "releaseId": rid, "keyId": app_key.key_id,
                                        "commit": commit, "recorded": "2026-01-01T00:00:00Z"}) + "\n")
        return env

    C1, C2, C3 = "a" * 40, "b" * 40, "c" * 40
    try:
        print("— first run: both channels seeded and announced —")
        publish("ada-cli", "0.1.58", 58, {"ada-macos-arm64.tar.gz": b"mac58" * 1000, "ada-linux-x64.tar.gz": b"lin58" * 1000,
                                          "install.sh": b"#!/bin/bash\necho installer 58\n"}, C1)
        publish("ada-ut", "0.7.4", 1, {"click": b"click74" * 500}, C2)
        rc, out = run()
        st = state()
        check("first check exits 0, records both channels", rc == 0 and st["recorded"]["ada-cli"]["sequence"] == 58
              and st["recorded"]["ada-ut"]["sequence"] == 1 and st["recorded"]["ada-cli"]["commit"] == C1, out)
        check("recording is announced (two ℹ️ messages), no alerts",
              len(fake.telegram) == 2 and all(m.startswith("ℹ️") and "RECORDED" in m for m in fake.telegram), fake.telegram)
        check("full hash performed on first record", set(st["full_hash_at"]) == {"ada-cli", "ada-ut"}
              and "full download of every asset matches" in out, out)
        check("installer identity + website resolution + page link + legacy manifest all verified",
              all(x in out for x in ("byte-identical", "website install URL resolves", "website page links", "legacy transition manifest agrees")), out)
        fake.telegram.clear()
        rc, out = run()
        check("second identical run is SILENT (no messages) and skips the daily full hash",
              rc == 0 and not fake.telegram and "full download" not in out and "matches the recorded authorized release" in out, out)

        print("— envelope integrity —")
        good = fake.envelopes["ada-cli"]
        t = json.loads(good); t["signature"] = ("A" if t["signature"][0] != "A" else "B") + t["signature"][1:]
        fake.envelopes["ada-cli"] = json.dumps(t).encode()
        rc, out = run()
        check("tampered live envelope → alert (ada-cli/envelope-invalid), exit 2",
              rc == 2 and any("envelope-invalid" in m for m in fake.telegram), fake.telegram)
        n = len(fake.telegram)
        rc, out = run()
        check("same failure within the re-alert window is NOT re-sent", len(fake.telegram) == n, fake.telegram[n:])
        set_state(lambda st: st["active"]["ada-cli/envelope-invalid"].__setitem__("last_sent", time.time() - 7 * 3600))
        rc, out = run()
        check("after realert_hours the failure is re-sent as STILL FAILING",
              len(fake.telegram) == n + 1 and "STILL FAILING" in fake.telegram[-1], fake.telegram[n:])
        fake.envelopes["ada-cli"] = good
        fake.telegram.clear()
        rc, out = run()
        check("recovery message when the envelope is good again", rc == 0 and len(fake.telegram) == 1
              and fake.telegram[0].startswith("✅") and "envelope-invalid" in fake.telegram[0], fake.telegram)
        fake.telegram.clear()
        fake.envelopes["ada-cli"] = envelope(TestKey("ada-cli"), "ada-cli", "0.1.58", 58,
                                             {"ada-macos-arm64.tar.gz": b"mac58" * 1000, "ada-linux-x64.tar.gz": b"lin58" * 1000, "install.sh": b"x"})
        rc, out = run()
        check("envelope signed by a DIFFERENT key → rejected", rc == 2 and any("envelope-invalid" in m for m in fake.telegram), fake.telegram)
        fake.envelopes["ada-cli"] = good
        run(); fake.telegram.clear()

        print("— rollback / sibling / new release —")
        older = envelope(cli_key, "ada-cli", "0.1.57", 57, {"ada-macos-arm64.tar.gz": b"mac57" * 1000})
        fake.assets[("ada-cli", "0.1.57", "ada-macos-arm64.tar.gz")] = b"mac57" * 1000
        fake.envelopes["ada-cli"] = older
        rc, out = run()
        check("latest URL replaying an OLDER valid envelope (GitHub still says 0.1.58) → rollback alert + latest-mismatch",
              rc == 2 and any("/rollback" in m and "sequence 57" in m for m in fake.telegram)
              and any("latest-mismatch" in m for m in fake.telegram), fake.telegram)
        fake.telegram.clear()
        fake.faults["api_status"] = 503
        rc, out = run()
        check("…rollback is still judged when the GitHub API is down", rc == 2 and "/rollback" in out, out)
        del fake.faults["api_status"]
        # deleted-release variant: 0.1.58 vanishes from GitHub, 0.1.57 is latest again
        saved_rel, saved_tag = fake.releases["test/ada-cli"].pop(), fake.tags["test/ada-cli"].pop("v0.1.58")
        fake.releases["test/ada-cli"].append({"id": 900, "tag_name": "v0.1.57", "draft": False, "immutable": True})
        fake.tags["test/ada-cli"]["v0.1.57"] = "9" * 40
        rc, out = run()
        check("release deleted wholesale (0.1.57 latest again) → rollback alert, record keeps 58",
              rc == 2 and "/rollback" in out and state()["recorded"]["ada-cli"]["sequence"] == 58, out)
        fake.releases["test/ada-cli"].pop(); fake.tags["test/ada-cli"].pop("v0.1.57")
        fake.releases["test/ada-cli"].append(saved_rel); fake.tags["test/ada-cli"]["v0.1.58"] = saved_tag
        fake.envelopes["ada-cli"] = good; run(); fake.telegram.clear()
        sibling = envelope(cli_key, "ada-cli", "0.1.58", 58, {"ada-macos-arm64.tar.gz": b"EVIL" * 1000, "ada-linux-x64.tar.gz": b"lin58" * 1000, "install.sh": b"#!/bin/bash\necho installer 58\n"})
        fake.envelopes["ada-cli"] = sibling
        rc, out = run()
        check("validly signed envelope with the SAME sequence but different assets → record-mismatch alert",
              rc == 2 and any("record-mismatch" in m and "assets" in m for m in fake.telegram), fake.telegram)
        fake.envelopes["ada-cli"] = good; run(); fake.telegram.clear()
        publish("ada-cli", "0.1.59", 59, {"ada-macos-arm64.tar.gz": b"mac59" * 1000, "ada-linux-x64.tar.gz": b"lin59" * 1000,
                                          "install.sh": b"#!/bin/bash\necho installer 59\n"}, C3, workflow="failure")
        rc, out = run()
        st = state()
        check("new release whose workflow FAILED → uncorroborated alert, NOT recorded (record stays 58)",
              rc == 2 and st["recorded"]["ada-cli"]["sequence"] == 58 and any("uncorroborated" in m for m in fake.telegram), fake.telegram)
        fake.telegram.clear()
        fake.runs["test/ada-cli"][-1].update({"conclusion": "success", "jobs": JOBS_OK})
        rc, out = run()
        st = state()
        check("same release once the workflow is green → recorded + announced + recovery for the previous alert",
              rc == 0 and st["recorded"]["ada-cli"]["sequence"] == 59 and st["recorded"]["ada-cli"]["commit"] == C3
              and any("RECORDED" in m and "v0.1.59" in m for m in fake.telegram)
              and any(m.startswith("✅") and "uncorroborated" in m for m in fake.telegram), fake.telegram)
        fake.telegram.clear()
        fake.tags["test/ada-cli"]["v0.1.59"] = "d" * 40
        rc, out = run()
        check("tag moved to another commit after recording → record-mismatch (commit)",
              rc == 2 and any("record-mismatch" in m and "commit" in m for m in fake.telegram), fake.telegram)
        fake.tags["test/ada-cli"]["v0.1.59"] = {"tag_sha": "e" * 40, "commit": C3}
        fake.telegram.clear()
        rc, out = run()
        check("annotated tag resolving to the recorded commit → clean (recovery only)",
              rc == 0 and len(fake.telegram) == 1 and fake.telegram[0].startswith("✅"), fake.telegram)
        fake.telegram.clear()

        print("— GitHub state —")
        fake.releases["test/ada-cli"].append({"id": 1099, "tag_name": "v0.1.60", "draft": False, "immutable": True})
        rc, out = run()
        check("a newer non-draft release while latest stays 0.1.59 → latest-mismatch/frozen alert",
              rc == 2 and any("latest" in m for m in fake.telegram), fake.telegram)
        fake.releases["test/ada-cli"].pop(); run(); fake.telegram.clear()
        fake.releases["test/ada-cli"][-1]["immutable"] = False
        rc, out = run()
        check("latest release not immutable → alert", rc == 2 and any("not-immutable" in m for m in fake.telegram), fake.telegram)
        fake.releases["test/ada-cli"][-1]["immutable"] = True; run(); fake.telegram.clear()
        fake.faults["api_status"] = 503
        rc, out = run()
        check("GitHub API unreachable → alert, never silent", rc == 2 and any("github-unreachable" in m for m in fake.telegram), fake.telegram)
        del fake.faults["api_status"]; run(); fake.telegram.clear()

        print("— assets —")
        fake.faults["asset_404"] = "ada-linux-x64.tar.gz"
        rc, out = run()
        check("asset missing at its immutable URL → asset-unreachable alert",
              rc == 2 and any("asset-unreachable" in m and "ada-linux-x64.tar.gz" in m for m in fake.telegram), fake.telegram)
        del fake.faults["asset_404"]; run(); fake.telegram.clear()
        fake.faults["range_total"] = 12345
        rc, out = run()
        check("server reporting a different total size → alert (Range probe)",
              rc == 2 and any("signed size" in m for m in fake.telegram), fake.telegram)
        del fake.faults["range_total"]; run(); fake.telegram.clear()
        fake.faults["asset_sub"] = {"ada-macos-arm64.tar.gz": b"mac59" * 999 + b"XXXXX"}
        rc, out = run()
        check("substituted bytes with the right size pass the hourly probe (documented limit)", rc == 0 and not fake.telegram, out)
        set_state(lambda st: st["full_hash_at"].__setitem__("ada-cli", 0))
        rc, out = run()
        check("…and are caught by the daily full download (asset-hash alert)",
              rc == 2 and any("asset-hash" in m for m in fake.telegram), fake.telegram)
        del fake.faults["asset_sub"]; run(); fake.telegram.clear()

        print("— installer + website —")
        fake.raw[("test/ada-cli", "v0.1.59", "scripts/get-ada.sh")] = b"#!/bin/bash\necho DIFFERENT\n"
        rc, out = run()
        check("released install.sh ≠ scripts/get-ada.sh@tag → installer alert",
              rc == 2 and any("/installer" in m and "differs" in m for m in fake.telegram), fake.telegram)
        fake.raw[("test/ada-cli", "v0.1.59", "scripts/get-ada.sh")] = b"#!/bin/bash\necho installer 59\n"; run(); fake.telegram.clear()
        fake.site_installer = b"#!/bin/bash\necho stale website copy\n"
        rc, out = run()
        check("website install URL serving different bytes → website-installer alert",
              rc == 2 and any("website-installer" in m for m in fake.telegram), fake.telegram)
        fake.site_installer = None; run(); fake.telegram.clear()
        fake.site_page = b"<html>no link here</html>"
        rc, out = run()
        check("app page not linking the released click → website-page alert",
              rc == 2 and any("website-page" in m for m in fake.telegram), fake.telegram)
        fake.site_page = ("<a href=\"%s/download/ada-ut/v0.7.4/click\">x</a>" % B).encode(); run(); fake.telegram.clear()
        fake.legacy_manifest = {"version": "0.7.3", "sha256": "00" * 32}
        rc, out = run()
        check("legacy Blob manifest disagreeing with the authoritative release → legacy-blob alert",
              rc == 2 and any("legacy-blob" in m for m in fake.telegram), fake.telegram)
        fake.legacy_manifest = {"version": "0.7.4", "sha256": sha(b"click74" * 500), "size": len(b"click74" * 500)}; run(); fake.telegram.clear()

        print("— app corroboration —")
        env75 = publish("ada-ut", "0.7.5", 2, {"click": b"click75" * 500}, "f" * 40, log=False)
        rc, out = run()
        st = state()
        check("new app release absent from the local publication log → uncorroborated, not recorded",
              rc == 2 and st["recorded"]["ada-ut"]["sequence"] == 1 and any("uncorroborated" in m and "publication log" in m for m in fake.telegram), fake.telegram)
        fake.telegram.clear()
        with open(pub_log, "a") as f:
            f.write(json.dumps({"tag": "v0.7.5", "version": "0.7.5", "sequence": 2, "clickSha256": "11" * 32,
                                "envelopeSha256": sha(fake.envelopes["ada-ut"]), "commit": "f" * 40}) + "\n")
        rc, out = run()
        check("publication log entry with a different click hash → still uncorroborated",
              rc == 2 and state()["recorded"]["ada-ut"]["sequence"] == 1, out)
        fake.telegram.clear()
        with open(pub_log, "a") as f:
            f.write(json.dumps({"tag": "v0.7.5", "version": "0.7.5", "sequence": 2, "clickSha256": sha(b"click75" * 500),
                                "envelopeSha256": sha(fake.envelopes["ada-ut"]), "commit": "f" * 40}) + "\n")
        rc, out = run()
        check("matching publication log entry → recorded + announced",
              rc == 0 and state()["recorded"]["ada-ut"]["sequence"] == 2 and any("RECORDED" in m and "v0.7.5" in m for m in fake.telegram), fake.telegram)
        fake.telegram.clear()

        print("— expiry —")
        fake.envelopes["ada-ut"] = envelope(app_key, "ada-ut", "0.7.5", 2, {"click": b"click75" * 500}, expires_days=10,
                                            published=state()["recorded"]["ada-ut"]["published"])
        rc, out = run()
        check("metadata expiring within the warning window → expiry alert (plus record mismatch on the changed expiry)",
              rc == 2 and any("/expiry" in m and "10.0 days" in m or "/expiry" in m and "9.9 days" in m for m in fake.telegram), fake.telegram)
        fake.telegram.clear()

        print("— delivery, locking, heartbeat —")
        fake.envelopes["ada-ut"] = env75      # back to the recorded envelope: expiry + mismatch recover
        run(); fake.telegram.clear()
        fake.faults["tg_status"] = 500
        fake.faults["asset_404"] = "click"
        rc, out = run()
        st = state()
        check("Telegram down while a NEW finding appears → message QUEUED in state, run still completes",
              rc == 2 and any("asset-unreachable" in m for m in st["queued"]) and st.get("last_completed"), out)
        del fake.faults["tg_status"]
        rc, out = run()
        check("queued message delivered on the next run", any("asset-unreachable" in m for m in fake.telegram) and not state()["queued"], fake.telegram)
        del fake.faults["asset_404"]; run(); fake.telegram.clear()
        import fcntl
        fd = os.open(os.path.join(state_dir, "state.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        rc, out = run()
        check("a concurrent run is refused while the lock is held", rc == 1 and "holds" in out, out)
        fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)
        fake.telegram.clear()
        rc, out = run("heartbeat")
        check("heartbeat with a fresh check → silent, exit 0", rc == 0 and not fake.telegram, out)
        set_state(lambda st: st.__setitem__("last_completed", time.time() - 4 * 3600))
        rc, out = run("heartbeat")
        check("heartbeat with a stale check → alert, exit 2", rc == 2 and len(fake.telegram) == 1 and "has not completed" in fake.telegram[0], fake.telegram)
        rc, out = run("heartbeat")
        check("stale heartbeat is not re-sent within the window", len(fake.telegram) == 1, fake.telegram)
        run("check"); fake.telegram.clear()
        rc, out = run("heartbeat")
        check("heartbeat recovery message once checks complete again", rc == 0 and len(fake.telegram) == 1 and fake.telegram[0].startswith("✅"), fake.telegram)
        rc, out = run("status")
        check("status prints the state", rc == 0 and '"recorded"' in out, out[-200:])
        check("bot token never appears in output or state", "tok" not in out.replace("token", "") and "tok" not in json.dumps(state()).replace("token", ""))
    finally:
        fake.close()
        shutil.rmtree(root, ignore_errors=True)

    print("\nwatch selftest: %d passed, %d failed" % (PASSED, FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
