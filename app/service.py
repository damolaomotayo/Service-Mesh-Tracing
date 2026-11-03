"""One tiny service for the whole demo. SERVICE selects the role: frontend, catalog or pricing.
Standard library only. Each service talks to the next one through its OWN sidecar (127.0.0.1:15001); it never
knows about versions, certificates or retries. That is what the mesh is for.

Chaos (changed at run time with scripts/chaos.sh): /tmp/chaos.json = {"delay_ms": 0, "error_rate": 0, "status": 500}
"""
import json
import os
import random
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE = os.environ.get("SERVICE", "frontend")
VERSION = os.environ.get("VERSION", "")
POD = os.environ.get("POD", SERVICE)
UPSTREAM = os.environ.get("UPSTREAM_URL", "http://127.0.0.1:15001")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
CHAOS_FILE = os.environ.get("CHAOS_FILE", "/tmp/chaos.json")
STATS_FILE = os.environ.get("STATS_FILE", "/tmp/stats.json")
# Headers a service must pass on, or the mesh cannot stitch the trace together or route by header.
FORWARD = ("traceparent", "tracestate", "x-request-id", "x-canary")

hits = {"requests": 0}


def chaos():
    try:
        with open(CHAOS_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_stats():
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as fh:
            json.dump(hits, fh)
    except OSError:
        pass


def call_upstream(path, headers):
    """Returns (status, parsed JSON or None). The sidecar does mTLS, routing, retries and timeouts."""
    req = urllib.request.Request(UPSTREAM + path, headers={k: v for k, v in headers.items() if k.lower() in FORWARD})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send(self, status, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/healthz":
            return self.send(200, b"ok\n", "text/plain")
        hits["requests"] += 1
        save_stats()
        c = chaos()
        if c.get("delay_ms"):
            time.sleep(int(c["delay_ms"]) / 1000)
        if c.get("error_rate") and random.random() < float(c["error_rate"]):
            return self.send(int(c.get("status", 500)), {"service": SERVICE, "pod": POD, "error": "injected failure"})
        handler = {"frontend": self.frontend, "catalog": self.catalog, "pricing": self.pricing}[SERVICE]
        handler(path)

    def pricing(self, path):
        self.send(200, {"service": "pricing", "pod": POD, "price": 42.0, "currency": "USD"})

    def catalog(self, path):
        status, price = call_upstream("/price", self.headers)
        body = {"service": "catalog", "pod": POD, "version": VERSION, "items": ["book", "pen", "lamp"]}
        if status == 200 and price:
            body.update(price=price["price"], currency=price["currency"], pricing="ok")
        else:                                                            # degrade gracefully, never fail the whole page
            body.update(price=None, pricing="unavailable", pricing_status=status)
        self.send(200, body)

    def frontend(self, path):
        if path in ("/", "/ui"):
            status, cat = call_upstream("/products", self.headers)
            ok = status == 200 and cat
            page = (f"<!doctype html><meta charset=utf-8><title>Mesh demo</title><body style='font:18px system-ui;margin:2rem'>"
                    f"<h1>Mesh demo</h1><p>frontend &rarr; catalog <b>{cat.get('version') if ok else '?'}</b> ({cat.get('pod') if ok else 'down'}) "
                    f"&rarr; pricing: <b>{cat.get('pricing') if ok else '?'}</b></p>"
                    f"<p>Refresh to see which version answers. Traces: port 16686.</p>").encode()
            return self.send(200 if ok else 502, page, "text/html; charset=utf-8")
        if path == "/api/products":
            status, cat = call_upstream("/products", self.headers)
            if status == 200 and cat:
                return self.send(200, {"service": "frontend", "pod": POD, "catalog": cat})
            return self.send(502, {"service": "frontend", "pod": POD, "error": "catalog unreachable", "catalog_status": status})
        self.send(404, {"error": "not found"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
