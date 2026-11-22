"""The web interface: one static page plus a small JSON API.

    GET  /metrics                          Prometheus metrics (see ``prometheus``)
    GET  /api/processes                    status of every process
    GET  /api/processes/<name>/logs        log lines (see ``_logs`` for parameters)
    GET  /api/processes/<name>/metrics     resource history (see ``_metrics``)
    GET  /api/processes/<name>/runs        recent runs, newest first
    POST /api/processes/<name>/<action>    action is start, stop or restart
    POST /api/reload                       re-read the config, Procfiles and .env files
    GET  /api/tsdb/metrics                 stored metrics, their kinds and label values
    GET  /api/tsdb/query                   series for a chart (see ``_query``)
    GET  /api/dashboards                   saved dashboards
    POST /api/dashboards                   create one: {"name": ..., "spec": {...}}
    GET|PUT|DELETE /api/dashboards/<id>    read, replace or delete one

Requests that change something need the X-Corral header (see CSRF_HEADER).
"""

from __future__ import annotations

import ipaddress
import json
import time
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, unquote, urlsplit

from . import prometheus
from .monitor import Monitor
from .store import STREAMS, LogStore
from .supervisor import Supervisor
from .config import ConfigError
from .tsdb import MetricDB, NotFound, QueryError

ACTIONS = ("start", "stop", "restart")
MAX_LIMIT = 5000
MAX_RANGE = 90 * 86400
MAX_BODY = 1024 * 1024

# Browsers only send custom headers cross-origin after a CORS preflight, which
# this server never approves, so requiring one stops other websites from
# pressing the buttons on behalf of someone who has the page reachable.
CSRF_HEADER = "X-Corral"


def _host_name(header: str) -> str:
    """The host part of a Host header, lower-cased, without port or brackets."""
    header = header.strip().lower()
    if header.startswith("["):
        return header[1:].partition("]")[0]
    if header.count(":") == 1:
        return header.partition(":")[0]
    return header


def _is_loopback(name: str) -> bool:
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def host_filter(bind_host: str, allowed_hosts=()) -> Callable[[str], bool] | None:
    """Which Host headers to answer, or None for any.

    A server listening only on loopback is reachable only from this machine,
    but a web page in a local browser can still point a host name of its own
    at 127.0.0.1 (DNS rebinding) and then read the API as a same-origin page.
    So it only answers to loopback names, plus ``allowed_hosts`` (for a reverse
    proxy that passes on its own host name). ``"*"`` turns the check off; so
    does listening on another address, unless ``allowed_hosts`` is given.
    """
    allowed = {h.strip().lower() for h in allowed_hosts}
    if "*" in allowed:
        return None
    if not allowed and not _is_loopback(_host_name(bind_host)):
        return None
    return lambda header: (name := _host_name(header)) in allowed or _is_loopback(name)


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


def _body(body: bytes):
    try:
        return json.loads(body)
    except ValueError:
        raise _Error(HTTPStatus.BAD_REQUEST, "the request body must be JSON") from None


class Handler(BaseHTTPRequestHandler):
    server_version = "corral"
    protocol_version = "HTTP/1.1"
    supervisor: Supervisor
    store: LogStore
    monitor: Monitor | None
    tsdb: MetricDB | None = None
    reload: Callable[[], dict] | None = None
    host_ok: Callable[[str], bool] | None = None

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

    def _dispatch(self, method: str, body: bytes = b"") -> None:
        url = urlsplit(self.path)
        parts = [unquote(p) for p in url.path.split("/") if p]
        try:
            host = self.headers.get("Host")
            if self.host_ok is not None and host is not None and not self.host_ok(host):
                raise _Error(
                    HTTPStatus.FORBIDDEN,
                    f"this server does not answer to the host name {host!r}; add it to allowed_hosts",
                )
            if method != "GET" and self.headers.get(CSRF_HEADER) is None:
                raise _Error(HTTPStatus.FORBIDDEN, f"missing {CSRF_HEADER} header")
            if parts[:2] in (["api", "tsdb"], ["api", "dashboards"]):
                self._stored(method, parts, parse_qs(url.query), body)
            elif method == "GET" and not parts:
                page = resources.files("corral").joinpath("static/index.html").read_bytes()
                self._send(HTTPStatus.OK, page, "text/html; charset=utf-8")
            elif method == "GET" and parts == ["metrics"]:
                text = prometheus.render(self.supervisor, self.monitor)
                self._send(HTTPStatus.OK, text.encode(), prometheus.CONTENT_TYPE)
            elif method == "POST" and parts == ["api", "reload"]:
                if self.reload is None:
                    raise _Error(HTTPStatus.NOT_FOUND, "reloading is not available")
                try:
                    self._json(self.reload())
                except ConfigError as exc:
                    raise _Error(HTTPStatus.BAD_REQUEST, str(exc)) from None
            elif method == "GET" and parts == ["api", "processes"]:
                self._json({"processes": [self._status(s) for s in self.supervisor.processes.values()]})
            elif len(parts) == 4 and parts[:2] == ["api", "processes"]:
                process = self.supervisor.processes.get(parts[2])
                if process is None:
                    raise _Error(HTTPStatus.NOT_FOUND, "no such process")
                if method == "GET" and parts[3] == "logs":
                    self._logs(process.cfg.name, parse_qs(url.query))
                elif method == "GET" and parts[3] == "metrics":
                    self._metrics(process.cfg.name, parse_qs(url.query))
                elif method == "GET" and parts[3] == "runs":
                    limit = _int(parse_qs(url.query), "limit") or 50
                    self._json({"runs": self.store.runs(process.cfg.name, max(1, min(limit, 1000)))})
                elif method == "POST" and parts[3] in ACTIONS:
                    getattr(process, parts[3])()
                    self._json(self._status(process))
                else:
                    raise _Error(HTTPStatus.NOT_FOUND, "not found")
            else:
                raise _Error(HTTPStatus.NOT_FOUND, "not found")
        except _Error as exc:
            self._json({"error": exc.message}, exc.status)

    def _stored(self, method: str, parts: list[str], params: dict, body: bytes) -> None:
        """Stored metrics and dashboards."""
        if self.tsdb is None:
            raise _Error(HTTPStatus.NOT_FOUND, "not found")
        db = self.tsdb
        try:
            if parts[1:] == ["tsdb", "metrics"] and method == "GET":
                self._json({"interval": db.interval, "metrics": db.catalog()})
            elif parts[1:] == ["tsdb", "query"] and method == "GET":
                self._query(params)
            elif parts[1:] == ["dashboards"] and method == "GET":
                self._json({"dashboards": db.dashboards()})
            elif parts[1:] == ["dashboards"] and method == "POST":
                self._json(db.create_dashboard(_body(body)), HTTPStatus.CREATED)
            elif len(parts) == 3 and parts[1] == "dashboards" and parts[2].isdigit():
                dashboard_id = int(parts[2])
                if method == "GET":
                    self._json(db.dashboard(dashboard_id))
                elif method == "PUT":
                    self._json(db.update_dashboard(dashboard_id, _body(body)))
                elif method == "DELETE":
                    db.delete_dashboard(dashboard_id)
                    self._json({"deleted": dashboard_id})
                else:
                    raise _Error(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")
            else:
                raise _Error(HTTPStatus.NOT_FOUND, "not found")
        except QueryError as exc:
            raise _Error(HTTPStatus.BAD_REQUEST, str(exc)) from None
        except NotFound as exc:
            raise _Error(HTTPStatus.NOT_FOUND, str(exc)) from None

    def _query(self, params: dict) -> None:
        """Parameters: ``q`` (the query as JSON: metric, match, fn, agg, by, q),
        ``range`` (seconds back from now) or ``start``/``end`` (Unix seconds),
        and ``points`` (roughly how many steps)."""
        try:
            spec = json.loads(params.get("q", [""])[-1])
        except ValueError:
            raise _Error(HTTPStatus.BAD_REQUEST, "q must be a JSON query") from None
        now = time.time()
        end = _int(params, "end") or now
        start = _int(params, "start")
        if start is None:
            start = end - (_int(params, "range") or 3600)
        if end - start > self.tsdb.retention + 86400:
            start = end - self.tsdb.retention
        self._json(self.tsdb.query(spec, start, end, _int(params, "points") or 300))

    def _status(self, process) -> dict:
        status = process.status()
        status["usage"] = self.monitor.latest(process.cfg.name) if self.monitor else None
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

    def _with_body(self, method: str) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            self.close_connection = True  # the body is not read, so the connection cannot be reused
            self._json({"error": "missing or too large request body"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        self._dispatch(method, self.rfile.read(length) if length else b"")

    def do_POST(self):
        self._with_body("POST")

    def do_PUT(self):
        self._with_body("PUT")

    def do_DELETE(self):
        self._with_body("DELETE")


def make_server(
    host: str,
    port: int,
    supervisor: Supervisor,
    store: LogStore,
    monitor: Monitor | None = None,
    allowed_hosts=(),
    tsdb: MetricDB | None = None,
    reload: Callable[[], dict] | None = None,
) -> ThreadingHTTPServer:
    check = host_filter(host, allowed_hosts)
    handler = type(
        "BoundHandler",
        (Handler,),
        # staticmethod, or the function would be bound to the handler instance
        {"supervisor": supervisor, "store": store, "monitor": monitor, "tsdb": tsdb,
         "host_ok": staticmethod(check) if check else None,
         "reload": staticmethod(reload) if reload else None},
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
