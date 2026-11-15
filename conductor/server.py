"""HTTP server: JSON API (see docs/api.md) plus the static UI.

Standard library only. ThreadingHTTPServer gives one thread per request;
all shared state lives in SQLite, so handlers need no extra locking beyond
what ``Service`` does per run.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .service import ActionError, Service

log = logging.getLogger("conductor.http")
STATIC = Path(__file__).parent / "static"

ROUTES: list[tuple[str, re.Pattern, str]] = [
    ("GET", re.compile(r"^/api/runs$"), "list_runs"),
    ("POST", re.compile(r"^/api/runs$"), "create_run"),
    ("GET", re.compile(r"^/api/runs/(\d+)$"), "get_run"),
    ("GET", re.compile(r"^/api/runs/(\d+)/diff$"), "get_diff"),
    ("GET", re.compile(r"^/api/runs/(\d+)/log$"), "get_log"),
    ("POST", re.compile(r"^/api/runs/(\d+)/(answer|restart|cancel|accept|reject|"
                        r"resolve_conflicts|retest)$"), "action"),
    ("GET", re.compile(r"^/api/repo$"), "repo"),
    ("GET", re.compile(r"^/api/stats$"), "stats"),
]


def make_handler(svc: Service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "conductor"

        def log_message(self, fmt, *args):  # quiet: polling would flood the console
            log.debug("%s " + fmt, self.address_string(), *args)

        # -- plumbing ------------------------------------------------------
        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj) -> None:
            self._send(status, json.dumps(obj, default=str).encode(), "application/json")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                data = json.loads(self.rfile.read(n))
            except ValueError:
                raise ActionError("request body is not valid JSON")
            if not isinstance(data, dict):
                raise ActionError("request body must be a JSON object")
            return data

        def _dispatch(self, method: str) -> None:
            url = urlparse(self.path)
            if method == "GET" and not url.path.startswith("/api/"):
                return self._static(url.path)
            if method == "POST" and not self._same_origin():
                return self._json(403, {"error": "cross-origin request refused"})
            for m, pat, name in ROUTES:
                match = pat.match(url.path)
                if m == method and match:
                    try:
                        status, obj = getattr(self, "h_" + name)(
                            *match.groups(), query=parse_qs(url.query))
                        return self._json(status, obj)
                    except ActionError as e:
                        return self._json(e.status, {"error": str(e)})
                    except Exception as e:  # noqa: BLE001
                        log.exception("handler %s failed", name)
                        return self._json(500, {"error": f"internal error: {e}"})
            self._json(404, {"error": "not found"})

        def _same_origin(self) -> bool:
            # The API can merge code on this machine; refuse POSTs from other
            # web pages (CSRF). Browsers always send Origin on cross-site POSTs.
            origin = self.headers.get("Origin")
            if not origin:
                return True
            host = self.headers.get("Host", "")
            return urlparse(origin).netloc == host

        def _static(self, path: str) -> None:
            if path in ("", "/"):
                path = "/index.html"
            rel = path.removeprefix("/static/").lstrip("/")
            f = (STATIC / rel).resolve()
            if STATIC.resolve() not in f.parents or not f.is_file():
                return self._json(404, {"error": "not found"})
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, f.read_bytes(), ctype)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        # -- handlers ------------------------------------------------------
        def h_list_runs(self, query):
            return 200, svc.list_view()

        def h_create_run(self, query):
            run = svc.create_run(self._body())
            log.info("created run #%s: %s", run["id"], run["title"])
            return 201, svc.detail(run["id"])

        def h_get_run(self, run_id, query):
            return 200, svc.detail(int(run_id))

        def h_get_diff(self, run_id, query):
            return 200, svc.diff(int(run_id))

        def h_get_log(self, run_id, query):
            attempt = query.get("attempt", [None])[0]
            return 200, svc.log(int(run_id), int(attempt) if attempt else None)

        def h_action(self, run_id, action, query):
            run_id = int(run_id)
            body = self._body()
            if action == "answer":
                svc.answer(run_id, body.get("text", ""))
            elif action == "restart":
                svc.restart(run_id, body.get("note"))
            elif action == "cancel":
                svc.cancel(run_id)
            elif action == "accept":
                svc.accept(run_id, body.get("mode", "merge"))
            elif action == "reject":
                svc.reject(run_id)
            elif action == "resolve_conflicts":
                svc.resolve_conflicts(run_id)
            elif action == "retest":
                svc.retest(run_id)
            log.info("run #%s: %s", run_id, action)
            return 200, svc.detail(run_id)

        def h_repo(self, query):
            return 200, svc.repo_info(query.get("path", [""])[0])

        def h_stats(self, query):
            return 200, svc.stats()

    return Handler


def serve(svc: Service, host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(svc))
    httpd.daemon_threads = True
    return httpd
