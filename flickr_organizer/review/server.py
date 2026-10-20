"""The review tool's local web server: serves static/ and a small JSON API over ReviewApp.

It listens on 127.0.0.1 only, checks the Host header (against DNS rebinding), and every
API request must carry a random per-run token, so other web pages can't drive it.
"""
from __future__ import annotations

import json
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

from .app import ReviewApp

STATIC = {"style.css": "text/css", "app.js": "text/javascript"}


def _static(name: str) -> str:
    return resources.files(__package__).joinpath("static", name).read_text(encoding="utf-8")


def make_server(app: ReviewApp, port: int = 8765) -> tuple[HTTPServer, str]:
    """The server (not yet serving) and its per-run API token. port=0 picks a free port."""
    token = secrets.token_urlsafe(24)
    hosts: set[str] = set()   # filled in once the port is known

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self, api: bool) -> bool:
            if self.headers.get("Host") not in hosts:          # blocks DNS rebinding
                self._send(403, {"error": "bad host"})
                return False
            if api and self.headers.get("X-Token") != token:   # blocks other pages
                self._send(403, {"error": "bad token"})
                return False
            return True

        def do_GET(self):
            url = urlparse(self.path)
            if not self._guard(api=url.path.startswith("/api/")):
                return
            if url.path == "/":
                return self._send(200, _static("index.html").replace("__TOKEN__", json.dumps(token)), "text/html")
            if url.path.startswith("/static/") and url.path[len("/static/"):] in STATIC:
                name = url.path[len("/static/"):]
                return self._send(200, _static(name), STATIC[name])
            if url.path == "/api/summary":
                return self._send(200, app.summary())
            if url.path == "/api/queue":
                qs = parse_qs(url.query)
                return self._send(200, app.queue(qs.get("status", ["pending"])[0], qs.get("sort", ["newest"])[0]))
            if url.path == "/api/jobs":
                return self._send(200, app.jobs_status(int(parse_qs(url.query).get("since", ["0"])[0])))
            if url.path == "/api/find":
                pid = app.find(parse_qs(url.query).get("q", [""])[0])
                return self._send(200 if pid else 404, {"photo_id": pid} if pid else {"error": "not in your library"})
            if url.path.startswith("/api/item/"):
                item = app.item(url.path.rsplit("/", 1)[1])
                return self._send(200 if item else 404, item or {"error": "not found"})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            url = urlparse(self.path)
            if not self._guard(api=True):
                return
            parts = url.path.strip("/").split("/")   # api/item/<id>/decide
            if len(parts) == 4 and parts[:2] == ["api", "item"] and parts[3] == "process":
                try:
                    return self._send(200, app.process(parts[2]))
                except KeyError:
                    return self._send(404, {"error": "not in your library"})
                except Exception as e:
                    return self._send(500, {"error": str(e)})
            if len(parts) == 4 and parts[:2] == ["api", "item"] and parts[3] == "decide":
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                try:
                    return self._send(200, app.decide(parts[2], body.get("choices", [])))
                except KeyError:
                    return self._send(404, {"error": "not in queue"})
                except Exception as e:  # surface Flickr/network errors in the page
                    return self._send(500, {"error": str(e)})
            self._send(404, {"error": "not found"})

    server = HTTPServer(("127.0.0.1", port), Handler)
    bound = server.server_address[1]
    hosts.update({f"127.0.0.1:{bound}", f"localhost:{bound}"})
    return server, token


def serve(app: ReviewApp, port: int = 8765, open_browser: bool = True) -> None:
    server, _ = make_server(app, port)
    url = f"http://127.0.0.1:{port}/"
    print(f"Review tool running at {url}  (Ctrl-C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        left = app.jobs.qsize()
        print(f"\nStopped.{f' {left} photo(s) still to apply; they resume next time you run review.' if left else ''}")
