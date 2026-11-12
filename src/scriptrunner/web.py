"""The web interface: one static page plus a small JSON API.

    GET  /metrics                          Prometheus metrics (see ``prometheus``)
    GET  /api/scripts                      status of every script
    GET  /api/scripts/<name>/logs          log lines (see ``_logs`` for parameters)
    GET  /api/scripts/<name>/metrics       resource history (see ``_metrics``)
    GET  /api/scripts/<name>/runs          recent runs, newest first
    POST /api/scripts/<name>/<action>      action is start, stop or restart
"""

from __future__ import annotations

import json
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, unquote, urlsplit

from . import prometheus
from .monitor import Monitor
from .store import STREAMS, LogStore
from .supervisor import Supervisor

ACTIONS = ("start", "stop", "restart")
MAX_LIMIT = 5000
MAX_RANGE = 90 * 86400

# Browsers only send custom headers cross-origin after a CORS preflight, which
# this server never approves, so requiring one stops other websites from
# pressing the buttons on behalf of someone who has the page reachable.
CSRF_HEADER = "X-Scriptrunner"


class _Error(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        self.status = status
        self.message = message


def _int(params: dict, key: str) -> int | None:
    if key not in params:
        return None
    try:
        return int(params[key][-1])
    except ValueError:
        raise _Error(HTTPStatus.BAD_REQUEST, f"{key} must be an integer") from None


class Handler(BaseHTTPRequestHandler):
    server_version = "scriptrunner"
    protocol_version = "HTTP/1.1"
    supervisor: Supervisor
    store: LogStore
    monitor: Monitor | None

    def log_message(self, format, *args):  # keep the journal quiet
        pass

    # -- plumbing --------------------------------------------------------

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _dispatch(self, method: str) -> None:
        url = urlsplit(self.path)
        parts = [unquote(p) for p in url.path.split("/") if p]
        try:
            if method == "GET" and not parts:
                page = resources.files("scriptrunner").joinpath("static/index.html").read_bytes()
                self._send(HTTPStatus.OK, page, "text/html; charset=utf-8")
            elif method == "GET" and parts == ["metrics"]:
                text = prometheus.render(self.supervisor, self.monitor)
                self._send(HTTPStatus.OK, text.encode(), prometheus.CONTENT_TYPE)
            elif method == "GET" and parts == ["api", "scripts"]:
                self._json({"scripts": [self._status(s) for s in self.supervisor.scripts.values()]})
            elif len(parts) == 4 and parts[:2] == ["api", "scripts"]:
                script = self.supervisor.scripts.get(parts[2])
                if script is None:
                    raise _Error(HTTPStatus.NOT_FOUND, "no such script")
                if method == "GET" and parts[3] == "logs":
                    self._logs(script.cfg.name, parse_qs(url.query))
                elif method == "GET" and parts[3] == "metrics":
                    self._metrics(script.cfg.name, parse_qs(url.query))
                elif method == "GET" and parts[3] == "runs":
                    limit = _int(parse_qs(url.query), "limit") or 50
                    self._json({"runs": self.store.runs(script.cfg.name, max(1, min(limit, 1000)))})
                elif method == "POST" and parts[3] in ACTIONS:
                    if self.headers.get(CSRF_HEADER) is None:
                        raise _Error(HTTPStatus.FORBIDDEN, f"missing {CSRF_HEADER} header")
                    getattr(script, parts[3])()
                    self._json(self._status(script))
                else:
                    raise _Error(HTTPStatus.NOT_FOUND, "not found")
            else:
                raise _Error(HTTPStatus.NOT_FOUND, "not found")
        except _Error as exc:
            self._json({"error": exc.message}, exc.status)

    def _status(self, script) -> dict:
        status = script.status()
        status["usage"] = self.monitor.latest(script.cfg.name) if self.monitor else None
        return status

    def _metrics(self, name: str, params: dict) -> None:
        """Parameters: ``range`` (seconds back from now, default one hour) and
        ``points`` (roughly how many steps to divide it into)."""
        span = max(60, min(_int(params, "range") or 3600, MAX_RANGE))
        points = max(10, min(_int(params, "points") or 120, 1000))
        interval = self.monitor.interval if self.monitor else 10.0
        # Never finer than the sampling interval, or steps would alternate
        # between having a sample and not.
        bucket = float(max(round(span / points), round(interval), 1))
        now = time.time()
        since = (int((now - span) / bucket) + 1) * bucket
        self._json(
            {
                "now": now,
                "since": since,
                "bucket": bucket,
                "points": self.store.metrics(name, since, bucket),
                "runs": self.store.run_summary(name, now - span),
            }
        )

    def _logs(self, name: str, params: dict) -> None:
        """Parameters: ``q`` (substring, case-insensitive), ``streams``
        (comma-separated), ``after``/``before`` (line ids), ``limit``."""
        streams = None
        if "streams" in params:
            streams = tuple(s for s in params["streams"][-1].split(",") if s)
            if not set(streams) <= set(STREAMS):
                raise _Error(HTTPStatus.BAD_REQUEST, f"streams must be among {', '.join(STREAMS)}")
        limit = _int(params, "limit") or 500
        lines = self.store.query(
            name,
            text=params.get("q", [""])[-1],
            streams=streams,
            after_id=_int(params, "after"),
            before_id=_int(params, "before"),
            limit=max(1, min(limit, MAX_LIMIT)),
        )
        self._json({"lines": lines})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        # Actions carry no body; drain whatever was sent to keep the connection usable.
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self._dispatch("POST")


def make_server(
    host: str, port: int, supervisor: Supervisor, store: LogStore, monitor: Monitor | None = None
) -> ThreadingHTTPServer:
    handler = type(
        "BoundHandler", (Handler,), {"supervisor": supervisor, "store": store, "monitor": monitor}
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
