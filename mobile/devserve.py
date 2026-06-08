"""Dev-only static server + API reverse proxy for the Flutter web build.

Serves build/web at / and forwards /api, /health, /metrics to the backend so
the browser app talks to the API same-origin (no CORS needed).

Usage: python3 devserve.py [listen_port] [backend_base]
"""

import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9000
BACKEND = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8001"
BACKEND_URL = urllib.parse.urlparse(BACKEND)
if BACKEND_URL.scheme not in {"http", "https"} or not BACKEND_URL.netloc:
    raise SystemExit("backend_base must be an http(s) URL")
WEB_ROOT = Path(__file__).parent / "build" / "web"
PROXY_PREFIXES = ("/api/", "/health", "/metrics")

_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
}

_CTYPES = {
    ".html": "text/html",
    ".js": "application/javascript",
    ".json": "application/json",
    ".css": "text/css",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".wasm": "application/wasm",
    ".otf": "font/otf",
    ".ttf": "font/ttf",
    ".map": "application/json",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter logs
        sys.stderr.write("[devserve] " + (fmt % args) + "\n")

    def _is_proxy(self):
        return self.path.startswith(PROXY_PREFIXES)

    def _proxy(self):
        body = None
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            body = self.rfile.read(length)
        req = urllib.request.Request(  # noqa: S310 - backend URL is http(s)-validated at startup.
            BACKEND + self.path, data=body, method=self.command
        )
        for k, v in self.headers.items():
            if k.lower() not in _HOP:
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                data = resp.read()
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in _HOP:
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            for k, v in e.headers.items():
                if k.lower() not in _HOP:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:  # noqa: BLE001
            msg = f'{{"detail":"proxy error: {exc}"}}'.encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def _static(self):
        rel = self.path.split("?", 1)[0].lstrip("/")
        target = (WEB_ROOT / rel).resolve()
        # SPA fallback to index.html for unknown routes / missing assets.
        if not str(target).startswith(str(WEB_ROOT.resolve())) or not target.is_file():
            target = WEB_ROOT / "index.html"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type", _CTYPES.get(target.suffix, "application/octet-stream")
        )
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._proxy() if self._is_proxy() else self._static()

    def do_POST(self):
        self._proxy() if self._is_proxy() else self.send_error(404)

    do_PATCH = do_PUT = do_DELETE = do_POST


if __name__ == "__main__":
    print(
        f"[devserve] serving {WEB_ROOT} on :{PORT}, proxying {PROXY_PREFIXES} -> {BACKEND}"
    )
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()  # noqa: S104
