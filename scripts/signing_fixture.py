"""Shared test fixture: throwaway Ed25519 release keys and signed envelopes
made through the SAME scripts the real publisher uses (scripts/release/),
so a test that passes here is a test of the production signing path.

Requires an Ed25519-capable openssl (scripts/release/openssl-resolve.sh
finds one; LibreSSL on macOS /usr/bin cannot sign Ed25519)."""

import json
import os
import subprocess
import tempfile
import threading
import http.server

HERE = os.path.dirname(os.path.abspath(__file__))
# Tests deliberately break PATH in places; the signing scripts still need
# openssl/python3/coreutils, so they always get the PATH we were born with.
_ORIG_PATH = os.environ.get("PATH", "/usr/bin:/bin")
REPO_ROOT = os.path.dirname(HERE)
RELEASE_SCRIPTS = os.path.join(HERE, "release")


class TestKey:
    """One generated key pair for a channel, living in a temp dir."""

    def __init__(self, channel):
        self.channel = channel
        self.dir = tempfile.mkdtemp(prefix="ada-ut-testkey-")
        subprocess.run([os.path.join(RELEASE_SCRIPTS, "release-keygen.sh"),
                        channel, self.dir], check=True, capture_output=True,
                       env=dict(os.environ, PATH=_ORIG_PATH))
        record = next(f for f in os.listdir(self.dir) if f.endswith(".json"))
        with open(os.path.join(self.dir, record)) as f:
            data = json.load(f)
        self.key_id = data["keyId"]
        self.pub_hex = data["publicKeyHex"]
        self.priv = os.path.join(self.dir, self.key_id + ".priv.pem")
        self.pub = os.path.join(self.dir, self.key_id + ".pub.pem")

    def keys(self):
        return {self.key_id: self.pub_hex}

    def sign(self, manifest_bytes, channel=None):
        """Envelope bytes for EXACT manifest bytes via sign-envelope.sh."""
        channel = channel or self.channel
        work = tempfile.mkdtemp(prefix="ada-ut-sign-")
        try:
            manifest_path = os.path.join(work, "manifest.json")
            out_path = os.path.join(work, "manifest.sig.json")
            with open(manifest_path, "wb") as f:
                f.write(manifest_bytes)
            env = dict(os.environ, EXPECTED_PUBKEY_PEM=self.pub, PATH=_ORIG_PATH)
            subprocess.run([os.path.join(RELEASE_SCRIPTS, "sign-envelope.sh"),
                            self.priv, channel, manifest_path, out_path],
                           check=True, capture_output=True, env=env)
            with open(out_path, "rb") as f:
                return f.read()
        finally:
            for name in ("manifest.json", "manifest.sig.json"):
                try:
                    os.unlink(os.path.join(work, name))
                except OSError:
                    pass
            os.rmdir(work)


def manifest_bytes(channel, version, sequence, platforms, published=None,
                   expires=None, schema=1, extra=None):
    """Deterministic manifest JSON bytes in the shape build-manifest.sh
    emits. `platforms` maps name -> {url, size, sha256}."""
    data = {
        "channel": channel,
        "expires": expires or "2099-01-01T00:00:00Z",
        "platforms": platforms,
        "published": published or "2026-01-01T00:00:00Z",
        "schema": schema,
        "sequence": sequence,
        "version": version,
    }
    if extra:
        data.update(extra)
    return json.dumps(data, indent=2, sort_keys=True).encode()


class FakeHost:
    """Tiny HTTP host: path -> (body bytes, declared Content-Length or None
    for honest). Lets tests serve envelopes, artifacts, and liars."""

    def __init__(self):
        self.routes = {}
        self.hits = []
        host = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                host.hits.append(self.path)
                route = host.routes.get(self.path)
                if route is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body, declared = route
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length",
                                 str(len(body) if declared is None else declared))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def put(self, path, body, declared_length=None):
        self.routes[path] = (body, declared_length)
        return self.base + path

    def close(self):
        self.server.shutdown()
        self.server.server_close()
