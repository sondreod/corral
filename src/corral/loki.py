"""Forwarding log lines to Grafana Loki, or anything that takes its push API
(VictoriaLogs, Grafana Cloud).

Every line gets the labels ``app``, ``process`` and ``stream`` plus those
from the config. Lines are collected for up to ``batch_wait`` and sent
together, gzipped, as JSON. While the server cannot be reached, up to
``max_buffer`` lines wait; past that the oldest are dropped. Lines are always
stored in SQLite as well, so the web interface never misses any.
"""

from __future__ import annotations

import gzip
import json
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, deque

from . import __version__
from .config import LokiConfig

MAX_BATCH = 1000  # lines per request
_MAX_BACKOFF = 60.0
_TIMEOUT = 10.0


class _Rejected(Exception):
    """The server refused the batch for good (a 4xx other than 429)."""


class LokiPusher:
    def __init__(self, cfg: LokiConfig):
        self.cfg = cfg
        self._buffer: deque[tuple[str, str, str, float]] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = False
        self._thread: threading.Thread | None = None
        self.sent = 0
        self.dropped: Counter[str] = Counter()  # by reason: buffer_full, rejected
        self.failures = 0  # pushes that failed and will be retried

    # -- called by the log readers ----------------------------------------

    def push(self, process: str, stream: str, line: str, ts: float) -> None:
        with self._lock:
            if len(self._buffer) >= self.cfg.max_buffer:
                self._buffer.popleft()
                self.dropped["buffer_full"] += 1
            self._buffer.append((process, stream, line, ts))
            if len(self._buffer) == MAX_BATCH:
                self._wake.set()

    def buffered(self) -> int:
        with self._lock:
            return len(self._buffer)

    # -- the sender thread ------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="loki", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Send what is left, giving up after ``timeout`` seconds."""
        self._stopping = True
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _take(self) -> list[tuple[str, str, str, float]]:
        with self._lock:
            n = min(len(self._buffer), MAX_BATCH)
            return [self._buffer.popleft() for _ in range(n)]

    def _loop(self) -> None:
        batch: list = []
        backoff = 0.0
        failing = False
        while True:
            if not self._stopping:
                self._wake.wait(backoff or self.cfg.batch_wait)
                self._wake.clear()
            stopping = self._stopping
            if not batch:
                batch = self._take()
            if not batch:
                if stopping:
                    return
                continue
            try:
                self._send(batch)
            except _Rejected as exc:
                with self._lock:
                    self.dropped["rejected"] += len(batch)
                print(f"corral: loki rejected {len(batch)} line(s): {exc}", flush=True)
            except OSError as exc:  # URLError, HTTPError (5xx, 429), timeouts
                self.failures += 1
                if stopping:
                    return
                if not failing:
                    print(f"corral: loki push failed, retrying: {_reason(exc)}", flush=True)
                failing = True
                backoff = min(max(backoff * 2, 1.0), _MAX_BACKOFF)
                continue
            else:
                self.sent += len(batch)
                if failing:
                    print("corral: loki push works again", flush=True)
                failing = False
            batch = []
            backoff = 0.0
            if self.buffered() >= MAX_BATCH:
                self._wake.set()  # more is waiting: don't sit out batch_wait

    def _send(self, batch: list[tuple[str, str, str, float]]) -> None:
        request = urllib.request.Request(
            self.cfg.url,
            data=gzip.compress(encode(batch, self.cfg.labels)),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
                "User-Agent": f"corral/{__version__}",
                **self.cfg.headers,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code != 429:
                body = exc.read(500).decode("utf-8", "replace").strip()
                raise _Rejected(f"HTTP {exc.code}: {body}" if body else f"HTTP {exc.code}") from None
            raise


def encode(batch: list[tuple[str, str, str, float]], extra: dict[str, str]) -> bytes:
    """The body of a push request: one stream per (process, stream), lines in
    the order they were printed."""
    streams: dict[tuple[str, str], list[list[str]]] = {}
    for process, stream, line, ts in batch:
        streams.setdefault((process, stream), []).append([str(int(ts * 1e9)), line])
    body = {
        "streams": [
            {
                "stream": {**extra, "app": process.partition(".")[0], "process": process, "stream": stream},
                "values": values,
            }
            for (process, stream), values in streams.items()
        ]
    }
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "replace")


def _reason(exc: OSError) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return str(exc.reason)
    return str(exc) or type(exc).__name__
