#!/usr/bin/env python3
"""Battery for scripts/publish_click.sh + scripts/release/publish-github-release.sh
against an in-process fake GitHub Releases API, download host and Blob store,
with fault injection. Runs the REAL scripts on a throwaway copy of this
repository whose app key is a generated test key.

    python3 scripts/publish_selftest.py
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
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "py"))
from signing_fixture import TestKey  # noqa: E402
import release_verify as rv  # noqa: E402

PASSED = FAILED = 0


def check(label, ok, detail=""):
    global PASSED, FAILED
    print("  %s %s%s" % ("✔" if ok else "✖", label,
                          "" if ok or not detail else " — " + str(detail)[-400:]))
    if ok:
        PASSED += 1
    else:
        FAILED += 1


class FakeGitHub:
    """Releases API + uploads + public download host + Blob, one server."""

    def __init__(self):
        self.releases = {}
        self.next_id = 100
        self.blob = {}
        self.hits = []
        self.faults = {}   # upload_fail: asset name; patch: "ambiguous"|"fail";
        #                   download_substitute: name -> bytes; latest_override: bytes
        gh = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body=b"", ctype="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _json(self, status, obj):
                self._send(status, json.dumps(obj).encode())

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _auth(self):
                return self.headers.get("Authorization") == "Bearer t0k"

            def _rel_json(self, r):
                return {"id": r["id"], "tag_name": r["tag_name"], "draft": r["draft"],
                        "name": r["name"], "immutable": r["immutable"],
                        "assets": [{"name": n} for n in r["assets"]]}

            def do_GET(self):
                gh.hits.append(("GET", self.path))
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                m = re.match(r"^/api/repos/([^/]+/[^/]+)/releases/tags/([^/]+)$", path)
                if m:
                    if not self._auth():
                        return self._json(401, {"message": "bad token"})
                    for r in gh.releases.values():
                        if r["tag_name"] == m.group(2) and not r["draft"]:
                            return self._json(200, self._rel_json(r))
                    return self._json(404, {"message": "Not Found"})
                m = re.match(r"^/api/repos/([^/]+/[^/]+)/releases/(\d+)$", path)
                if m:
                    r = gh.releases.get(int(m.group(2)))
                    return self._json(200, self._rel_json(r)) if r else self._json(404, {})
                m = re.match(r"^/api/repos/([^/]+/[^/]+)/releases$", path)
                if m:
                    page = int(urllib.parse.parse_qs(parsed.query).get("page", ["1"])[0])
                    items = [self._rel_json(r) for r in gh.releases.values()] if page == 1 else []
                    return self._json(200, items)
                if path == "/latest/manifest.sig.json":
                    if "latest_override" in gh.faults:
                        return self._send(200, gh.faults["latest_override"])
                    latest = gh.latest()
                    if latest and "manifest.sig.json" in latest["assets"]:
                        return self._send(200, latest["assets"]["manifest.sig.json"])
                    return self._send(404, b"")
                m = re.match(r"^/download/(v[^/]+)/([^/]+)$", path)
                if m:
                    for r in gh.releases.values():
                        if r["tag_name"] == m.group(1) and not r["draft"] and m.group(2) in r["assets"]:
                            sub = gh.faults.get("download_substitute", {})
                            return self._send(200, sub.get(m.group(2), r["assets"][m.group(2)]))
                    return self._send(404, b"")
                m = re.match(r"^/blobpub/(.+)$", path)
                if m:
                    blob = gh.blob.get(m.group(1))
                    return self._send(200, blob) if blob is not None else self._send(404, b"")
                self._send(404, b"")

            def do_POST(self):
                gh.hits.append(("POST", self.path))
                body = self._body()
                parsed = urllib.parse.urlparse(self.path)
                if not self._auth():
                    return self._json(401, {"message": "bad token"})
                m = re.match(r"^/api/repos/([^/]+/[^/]+)/releases$", parsed.path)
                if m:
                    req = json.loads(body)
                    rid = gh.next_id
                    gh.next_id += 1
                    gh.releases[rid] = {"id": rid, "tag_name": req["tag_name"], "draft": bool(req.get("draft")),
                                        "name": req.get("name", ""), "immutable": False, "assets": {},
                                        "order": []}
                    return self._json(201, self._rel_json(gh.releases[rid]))
                m = re.match(r"^/api/repos/([^/]+/[^/]+)/releases/(\d+)/assets$", parsed.path)
                if m:
                    name = urllib.parse.parse_qs(parsed.query)["name"][0]
                    r = gh.releases.get(int(m.group(2)))
                    if r is None or not r["draft"]:
                        return self._json(422, {"message": "not a draft"})
                    if gh.faults.get("upload_fail") == name:
                        return self._json(500, {"message": "injected upload failure"})
                    r["assets"][name] = body
                    r["order"].append(name)
                    return self._json(201, {"name": name})
                self._json(404, {})

            def do_PATCH(self):
                gh.hits.append(("PATCH", self.path))
                body = json.loads(self._body())
                m = re.match(r"^/api/repos/([^/]+/[^/]+)/releases/(\d+)$", self.path)
                r = gh.releases.get(int(m.group(2))) if m else None
                if r is None:
                    return self._json(404, {})
                mode = gh.faults.get("patch")
                if mode == "fail":
                    return self._json(502, {"message": "injected: not applied"})
                if body.get("draft") is False:
                    r["draft"] = False
                    r["immutable"] = gh.faults.get("immutable", True)
                if mode == "ambiguous":
                    return self._json(502, {"message": "injected: applied but timed out"})
                self._json(200, self._rel_json(r))

            def do_DELETE(self):
                gh.hits.append(("DELETE", self.path))
                m = re.match(r"^/api/repos/([^/]+/[^/]+)/releases/(\d+)$", self.path)
                r = gh.releases.pop(int(m.group(2)), None) if m else None
                self._send(204 if r else 404, b"")

            def do_PUT(self):
                gh.hits.append(("PUT", self.path))
                body = self._body()
                m = re.match(r"^/blob/(.+)$", self.path)
                if m and self.headers.get("Authorization") == "Bearer b10b":
                    gh.blob[urllib.parse.unquote(m.group(1))] = body
                    return self._json(200, {"url": "ok"})
                self._json(403, {})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def latest(self):
        published = [r for r in self.releases.values() if not r["draft"]]
        return published[-1] if published else None

    def by_tag(self, tag):
        return next((r for r in self.releases.values() if r["tag_name"] == tag), None)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def main():
    root = tempfile.mkdtemp(prefix="ada-ut-publish-selftest-")
    repo_src = os.path.dirname(HERE)
    repo = os.path.join(root, "repo")
    os.makedirs(repo)
    for item in ("manifest.json", "LICENSE", ".gitignore", "py", "qml", "click", "assets", "scripts", "clickable.yaml"):
        src = os.path.join(repo_src, item)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(repo, item),
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elif os.path.exists(src):
            shutil.copy2(src, os.path.join(repo, item))
    key = TestKey("ada-ut")
    other = TestKey("ada-ut")
    os.makedirs(os.path.join(repo, ".release-keys"))
    shutil.copy2(key.pub, os.path.join(repo, ".release-keys", "ada-ut-release.pub.pem"))
    rv_path = os.path.join(repo, "py", "release_verify.py")

    def stamp(sequence, version, key_id=key.key_id, pub_hex=key.pub_hex):
        s = open(rv_path).read()
        s = re.sub(r"# STAMP-APP-KEY-BEGIN.*?# STAMP-APP-KEY-END",
                   '# STAMP-APP-KEY-BEGIN\nAPP_KEYS = {\n    "%s":\n        "%s",\n}\n# STAMP-APP-KEY-END'
                   % (key_id, pub_hex), s, flags=re.S)
        s = re.sub(r"^APP_RELEASE_SEQUENCE = \d+$", "APP_RELEASE_SEQUENCE = %d" % sequence, s, flags=re.M)
        open(rv_path, "w").write(s)
        m = json.load(open(os.path.join(repo, "manifest.json")))
        m["version"] = version
        json.dump(m, open(os.path.join(repo, "manifest.json"), "w"), indent=2)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "stamp", "--allow-empty"],
                       cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    stamp(1, "0.7.4")

    gh = FakeGitHub()
    log = os.path.join(root, "publications.jsonl")
    base_env = {
        "REPO_ROOT": repo, "REPO": "test/ada-ut", "GH_TOKEN": "t0k",
        "GH_API_URL": gh.base + "/api", "GH_UPLOADS_URL": gh.base + "/api",
        "PUBLIC_DOWNLOAD_BASE": gh.base + "/download",
        "LIVE_ENVELOPE_URL": gh.base + "/latest/manifest.sig.json",
        "SIGNING_KEY": key.priv, "PUBLICATION_LOG": log, "PUBLIC_RETRY_SLEEP": "0.05",
        "BLOB_API_URL": gh.base + "/blob", "BLOB_PUBLIC_PREFIX": gh.base + "/blobpub/app",
        "BLOB_TOKEN": "b10b", "HOME": root,
    }

    def run(*args, **env):
        e = dict(os.environ)
        e.update(base_env)
        e.update(env)
        p = subprocess.run([os.path.join(repo_src, "scripts", "publish_click.sh"), *args],
                           capture_output=True, text=True, env=e)
        return p.returncode, p.stdout + p.stderr

    def posts():
        return [h for h in gh.hits if h[0] == "POST"]

    def log_lines():
        return open(log).read().splitlines() if os.path.exists(log) else []

    def verify_public(seq, version):
        policy = rv.ReleasePolicy("ada-ut", key.keys(), gh.base + "/latest/manifest.sig.json",
                                  gh.base + "/download/v{version}/", 1)
        rv.TRUST_FILE = os.path.join(root, "trust.json")
        try:
            m = rv.resolve_release(policy)
        except rv.ReleaseVerifyError as exc:
            return False, exc
        return m["sequence"] == seq and m["version"] == version, m

    try:
        print("— gates —")
        rc, out = run()
        check("no live release + no --bootstrap → refused, nothing posted",
              rc != 0 and "only the one-time first signed release" in out and not posts(), out)
        rc, out = run("--bootstrap", "--dry-run")
        check("--dry-run signs + verifies, publishes nothing",
              rc == 0 and "dry run" in out and "the app's verifier" in out and not posts(), out)
        open(os.path.join(repo, "stray.txt"), "w").write("x")
        rc, out = run("--bootstrap")
        check("dirty tree refused", rc != 0 and "not clean" in out and not posts(), out)
        rc, out = run("--bootstrap", "--dry-run", "--allow-dirty")
        check("--allow-dirty lets a dry run through", rc == 0, out)
        os.unlink(os.path.join(repo, "stray.txt"))
        rc, out = run("--bootstrap", "0.9.9")
        check("version argument != manifest.json refused", rc != 0 and "!= manifest.json" in out, out)
        rc, out = run("--bootstrap", EXPECTED_PUB=other.pub, SIGNING_KEY=other.priv)
        check("committed key not pinned in release_verify.py → refused",
              rc != 0 and "does not pin" in out and not posts(), out)
        os.chmod(key.priv, 0o644)
        rc, out = run("--bootstrap")
        check("signing key with loose permissions refused", rc != 0 and "0600" in out, out)
        os.chmod(key.priv, 0o600)

        print("— bootstrap publish —")
        rc, out = run("--bootstrap")
        rel = gh.by_tag("v0.7.4")
        check("--bootstrap publishes v0.7.4", rc == 0 and rel is not None and rel["draft"] is False, out)
        check("assets uploaded in order, envelope last",
              rel and rel["order"] == ["ada.permaevidence_0.7.4_all.click", "manifest.json", "manifest.sig.json"],
              rel and rel["order"])
        ok, m = verify_public(1, "0.7.4")
        check("public envelope verifies with the app's verifier (seq 1, v0.7.4)", ok, str(m)[:200])
        click_built = open(os.path.join(repo, "build", "ada.permaevidence_0.7.4_all.click"), "rb").read()
        check("published click == built click, and matches the signed sha256",
              rel["assets"]["ada.permaevidence_0.7.4_all.click"] == click_built
              and m["platforms"]["click"]["sha256"] == hashlib.sha256(click_built).hexdigest())
        check("publication recorded after verification",
              len(log_lines()) == 1 and json.loads(log_lines()[0])["sequence"] == 1)
        check("release PATCHed with explicit make_latest",
              any(h[0] == "PATCH" for h in gh.hits))

        print("— supersession —")
        rc, out = run()
        check("same sequence again → superseded", rc != 0 and "superseded" in out, out)
        stamp(2, "0.7.4")
        rc, out = run()
        check("higher sequence but same tag → immutability refuses republish",
              rc != 0 and "already exists" in out, out)
        stamp(2, "0.7.5")
        rc, out = run("--bootstrap")
        check("--bootstrap with a live release refused", rc != 0 and "live signed release exists" in out, out)
        rc, out = run()
        check("v0.7.5 seq 2 publishes normally", rc == 0 and gh.by_tag("v0.7.5") and not gh.by_tag("v0.7.5")["draft"], out)
        ok, m = verify_public(2, "0.7.5")
        check("latest is now v0.7.5 seq 2", ok)
        rv.TRUST_FILE = os.path.join(root, "trust.json")
        rv.record_accepted(rv.ReleasePolicy("ada-ut", key.keys(), "", gh.base + "/download/v{version}/", 1), m)
        gh.faults["latest_override"] = gh.by_tag("v0.7.4")["assets"]["manifest.sig.json"]
        ok, replayed = verify_public(1, "0.7.4")
        check("rollback via a replayed older envelope refused",
              not ok and isinstance(replayed, rv.ReleaseVerifyError) and replayed.kind == "rollback", str(replayed))
        del gh.faults["latest_override"]

        print("— fault injection —")
        stamp(3, "0.7.6")
        stale_id = gh.next_id
        gh.releases[stale_id] = {"id": stale_id, "tag_name": "v0.7.6", "draft": True, "name": "stale",
                                 "immutable": False, "assets": {}, "order": []}
        gh.next_id += 1
        rc, out = run()
        check("stale draft for the tag is deleted, then v0.7.6 publishes",
              rc == 0 and stale_id not in gh.releases and ("DELETE", "/api/repos/test/ada-ut/releases/%d" % stale_id) in gh.hits
              and gh.by_tag("v0.7.6") and not gh.by_tag("v0.7.6")["draft"], out)
        stamp(4, "0.7.7")
        gh.faults["upload_fail"] = "manifest.sig.json"
        before = len(log_lines())
        rc, out = run()
        drafts = [r for r in gh.releases.values() if r["tag_name"] == "v0.7.7"]
        check("envelope upload failure → exit 1, draft left, old release stays latest, nothing recorded",
              rc != 0 and "uploading manifest.sig.json failed" in out and drafts and drafts[0]["draft"]
              and gh.latest()["tag_name"] == "v0.7.6" and len(log_lines()) == before, out)
        del gh.faults["upload_fail"]
        rc, out = run()
        check("retry cleans the stale draft and publishes v0.7.7",
              rc == 0 and len([r for r in gh.releases.values() if r["tag_name"] == "v0.7.7"]) == 1
              and gh.latest()["tag_name"] == "v0.7.7", out)
        stamp(5, "0.7.8")
        gh.faults["patch"] = "ambiguous"
        rc, out = run()
        check("ambiguous publish PATCH (applied, 502) → confirmed by re-read, success with warning",
              rc == 0 and "confirmed by re-read" in out and gh.latest()["tag_name"] == "v0.7.8", out)
        del gh.faults["patch"]
        stamp(6, "0.7.9")
        gh.faults["patch"] = "fail"
        before = len(log_lines())
        rc, out = run()
        check("publish PATCH failure (not applied) → exit 1, still a draft, nothing recorded",
              rc != 0 and "remains a draft" in out and gh.by_tag("v0.7.9")["draft"] and len(log_lines()) == before, out)
        del gh.faults["patch"]
        rc, out = run()
        check("retry after PATCH failure publishes v0.7.9", rc == 0 and gh.latest()["tag_name"] == "v0.7.9", out)
        stamp(7, "0.7.10")
        good_latest = gh.latest()["assets"]["manifest.sig.json"]
        tampered = json.loads(good_latest)
        tampered["signature"] = ("B" if tampered["signature"][0] != "B" else "C") + tampered["signature"][1:]
        gh.faults["latest_override"] = json.dumps(tampered).encode()
        rc, out = run()
        check("tampered LIVE envelope → hard stop (never unlocks bootstrap), nothing posted",
              rc != 0 and "hard stop" in out and not gh.by_tag("v0.7.10"), out)
        rc, out = run("--bootstrap")
        check("…even with --bootstrap", rc != 0 and "hard stop" in out and not gh.by_tag("v0.7.10"), out)
        del gh.faults["latest_override"]
        gh.faults["download_substitute"] = {"ada.permaevidence_0.7.10_all.click": b"not the click"}
        before = len(log_lines())
        rc, out = run()
        check("public click differs from the built one → exit 1, NOT recorded",
              rc != 0 and "public click differs" in out and len(log_lines()) == before, out)
        del gh.faults["download_substitute"]
        stamp(8, "0.7.11")
        gh.faults["immutable"] = False
        before = len(log_lines())
        rc, out = run()
        check("release not immutable → exit 1, NOT recorded",
              rc != 0 and "NOT immutable" in out and len(log_lines()) == before, out)
        del gh.faults["immutable"]

        print("— legacy Blob dual-publish —")
        stamp(9, "0.7.12")
        rc, out = run("--legacy-blob")
        legacy = json.loads(gh.blob.get("app/manifest.json", b"{}"))
        click_built = open(os.path.join(repo, "build", "ada.permaevidence_0.7.12_all.click"), "rb").read()
        check("--legacy-blob publishes the click + legacy manifest for apps ≤ 0.7.3",
              rc == 0 and gh.blob.get("app/ada.permaevidence_0.7.12_all.click") == click_built
              and legacy.get("version") == "0.7.12" and legacy.get("sha256") == hashlib.sha256(click_built).hexdigest()
              and legacy.get("size") == len(click_built) and legacy.get("filename") == "ada.permaevidence_0.7.12_all.click", out)
        gh.blob["app/manifest.json"] = json.dumps({"version": "9.9.9"}).encode()
        stamp(10, "0.7.13")
        rc, out = run("--legacy-blob")
        check("newer legacy manifest live → GitHub publishes, Blob left alone",
              rc == 0 and "left alone" in out and gh.latest()["tag_name"] == "v0.7.13"
              and json.loads(gh.blob["app/manifest.json"])["version"] == "9.9.9", out)
        check("every publication in the log is monotonic",
              [json.loads(l)["sequence"] for l in log_lines()] == sorted({json.loads(l)["sequence"] for l in log_lines()}),
              log_lines())
        check("the token never appears in output", "t0k" not in out and "b10b" not in out)
    finally:
        gh.close()
        shutil.rmtree(root, ignore_errors=True)

    print("\npublish selftest: %d passed, %d failed" % (PASSED, FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
