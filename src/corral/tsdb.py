"""Stored metrics and saved dashboards.

A ``Scraper`` thread fetches corral's own ``/metrics`` endpoint once per
``scrape_interval`` (which includes every script's own metrics) and stores
each sample in a separate SQLite database, ``metrics.sqlite3``. Samples are
kept for ``retention`` seconds. ``MetricDB.query`` turns them into series for
the dashboards; the dashboards themselves are stored in the same database as
JSON documents.

Timestamps are whole seconds, aligned to the scrape interval, so a series has
at most one sample per interval.
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
import threading
import time
import urllib.request
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from . import exposition

_SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    id     INTEGER PRIMARY KEY,
    name   TEXT NOT NULL,          -- sample name, e.g. corral_script_runs_total
    labels TEXT NOT NULL,          -- JSON object, keys sorted
    family TEXT NOT NULL,          -- the # TYPE name it belongs to
    kind   TEXT NOT NULL,          -- counter, gauge, bucket or untyped
    help   TEXT NOT NULL,
    UNIQUE (name, labels)
);
CREATE TABLE IF NOT EXISTS samples (
    series INTEGER NOT NULL,
    ts     INTEGER NOT NULL,
    value  REAL NOT NULL,
    PRIMARY KEY (series, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts);
CREATE TABLE IF NOT EXISTS dashboards (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL,
    spec    TEXT NOT NULL,         -- JSON: {"range": seconds, "panels": [...]}
    created REAL NOT NULL,
    updated REAL NOT NULL
);
"""

FUNCTIONS = ("raw", "rate", "per_minute", "increase", "quantile", "heatmap")
AGGREGATIONS = ("none", "sum", "avg", "min", "max", "count")
PANEL_TYPES = ("line", "stat", "bar", "table", "heatmap")
GRID_COLUMNS = 12
MAX_SERIES = 1000  # series one query may read
MAX_POINTS = 1500
MAX_PANELS = 100
MAX_SPEC_BYTES = 256 * 1024
MAX_LABEL_VALUES = 200  # per label in the catalogue


class QueryError(ValueError):
    pass


class NotFound(LookupError):
    pass


def _aggregate(how: str, values: list[float]) -> float | None:
    if not values:
        return None
    if how == "sum":
        return math.fsum(values)
    if how == "avg":
        return math.fsum(values) / len(values)
    if how == "min":
        return min(values)
    if how == "max":
        return max(values)
    return float(len(values))  # count


def _le(labels: dict[str, str]) -> float | None:
    try:
        return float(labels["le"])
    except (KeyError, ValueError):
        return None


def histogram_quantile(q: float, buckets: list[tuple[float, float]]) -> float | None:
    """The ``q`` quantile from cumulative ``(le, count)`` buckets, interpolating
    linearly inside a bucket, the way Prometheus does."""
    buckets = sorted(buckets)
    if not buckets or not math.isinf(buckets[-1][0]):
        return None
    total = buckets[-1][1]
    if not total > 0:
        return None
    rank = q * total
    lower, below = 0.0, 0.0
    for i, (le, count) in enumerate(buckets):
        if count >= rank:
            if math.isinf(le):
                return buckets[i - 1][0] if i else None
            if i == 0 and le <= 0:
                return le
            if count == below:
                return le
            return lower + (le - lower) * (rank - below) / (count - below)
        lower, below = le, count
    return buckets[-2][0] if len(buckets) > 1 else None


class MetricDB:
    def __init__(self, path: str | Path, interval: float = 60.0, retention: float = 30 * 86400):
        self.path = str(path)
        self.interval = max(1, int(round(interval)))
        self.retention = retention
        self._ids: dict[tuple[str, str], int] = {}
        self._write_lock = threading.Lock()
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # -- writing ---------------------------------------------------------

    def ingest(self, ts: int, samples: list[exposition.Sample]) -> int:
        """Store one scrape; returns the number of samples stored."""
        with self._write_lock, closing(self._connect()) as conn, conn:
            rows = []
            for s in samples:
                labels = json.dumps(dict(s.labels), separators=(",", ":"))
                key = (s.name, labels)
                sid = self._ids.get(key)
                if sid is None:
                    conn.execute(
                        "INSERT INTO series (name, labels, family, kind, help) VALUES (?, ?, ?, ?, ?)"
                        " ON CONFLICT (name, labels) DO UPDATE SET"
                        " family = excluded.family, kind = excluded.kind, help = excluded.help",
                        (s.name, labels, s.family, s.kind, s.help),
                    )
                    sid = conn.execute(
                        "SELECT id FROM series WHERE name = ? AND labels = ?", key
                    ).fetchone()[0]
                    self._ids[key] = sid
                rows.append((sid, ts, s.value))
            conn.executemany("INSERT OR REPLACE INTO samples (series, ts, value) VALUES (?, ?, ?)", rows)
        return len(rows)

    def prune(self, now: float | None = None) -> None:
        cutoff = int((time.time() if now is None else now) - self.retention)
        with self._write_lock, closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            conn.execute(
                "DELETE FROM series WHERE NOT EXISTS (SELECT 1 FROM samples WHERE samples.series = series.id)"
            )
            self._ids.clear()

    # -- reading ---------------------------------------------------------

    def catalog(self) -> list[dict]:
        """Every stored metric with its kind, help text and label values."""
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT name, kind, help, labels FROM series ORDER BY name").fetchall()
        metrics: dict[str, dict] = {}
        for name, kind, help, labels in rows:
            entry = metrics.setdefault(name, {"name": name, "kind": kind, "help": help, "labels": defaultdict(set)})
            for k, v in json.loads(labels).items():
                entry["labels"][k].add(v)
        for entry in metrics.values():
            entry["labels"] = {
                k: sorted(v, key=lambda x: (_le({"le": x}) is None, _le({"le": x}) or 0, x))[:MAX_LABEL_VALUES]
                for k, v in sorted(entry["labels"].items())
            }
        return list(metrics.values())

    def _step(self, span: float, points: int) -> int:
        return self.interval * max(1, math.ceil(span / points / self.interval))

    def query(self, spec: dict, start: float, end: float, points: int = 300) -> dict:
        spec = _check_query(spec)
        if not end > start:
            raise QueryError("end must be after start")
        points = max(2, min(int(points), MAX_POINTS))
        step = self._step(end - start, points)
        first, last = int(start // step), int(end // step)
        times = [b * step for b in range(first, last + 1)]
        fn = spec["fn"]

        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT id, labels, kind FROM series WHERE name = ?", (spec["metric"],)).fetchall()
            match = spec["match"]
            chosen = {}
            for sid, labels, kind in rows:
                labels = json.loads(labels)
                if all(labels.get(k) == v for k, v in match.items()):
                    chosen[sid] = labels
            if fn in ("quantile", "heatmap") and any(kind != "bucket" for _, _, kind in rows):
                raise QueryError(f"{fn} needs a histogram's _bucket metric")
            if len(chosen) > MAX_SERIES:
                raise QueryError(f"the query matches {len(chosen)} series; narrow it down (at most {MAX_SERIES})")
            values = self._fetch(conn, list(chosen), fn, step, first, last) if chosen else {}

        def aligned(per_bucket: dict[int, float]) -> list[float | None]:
            return [per_bucket.get(b) for b in range(first, last + 1)]

        if fn == "quantile":
            series = self._quantiles(chosen, values, spec, first, last)
        elif fn == "heatmap":
            series = self._heatmap(chosen, values, first, last)
        elif spec["agg"] == "none":
            series = [{"labels": chosen[sid], "values": aligned(values.get(sid, {}))} for sid in chosen]
        else:
            groups: dict[tuple, list[int]] = defaultdict(list)
            for sid, labels in chosen.items():
                groups[tuple(labels.get(k, "") for k in spec["by"])].append(sid)
            series = []
            for key, sids in groups.items():
                column = [
                    _aggregate(spec["agg"], [v for sid in sids if (v := values.get(sid, {}).get(b)) is not None])
                    for b in range(first, last + 1)
                ]
                series.append({"labels": dict(zip(spec["by"], key)), "values": column})

        series = [s for s in series if any(v is not None for v in s["values"])]
        series.sort(key=lambda s: sorted(s["labels"].items()))
        for s in series:
            s["values"] = [[t, v] for t, v in zip(times, s["values"])]
        return {"start": times[0], "end": end, "step": step, "series": series}

    def _fetch(self, conn, ids: list[int], fn: str, step: int, first: int, last: int) -> dict[int, dict[int, float]]:
        marks = ",".join("?" * len(ids))
        lo, hi = first * step, (last + 1) * step
        if fn == "raw":
            # SQLite returns `value` from the row holding MAX(ts): the last sample in each step.
            rows = conn.execute(
                f"SELECT series, ts / ? AS b, MAX(ts), value FROM samples"
                f" WHERE series IN ({marks}) AND ts >= ? AND ts < ? GROUP BY series, b",
                (step, *ids, lo, hi),
            ).fetchall()
            out: dict[int, dict[int, float]] = defaultdict(dict)
            for sid, b, _, v in rows:
                out[sid][b] = v
            return out
        # Counters: add up the increases between consecutive samples. A drop
        # means the counter restarted from zero (corral was restarted), so
        # the new value is all increase. The sample before the range is needed
        # for the first step's increase.
        lookback = step + 10 * self.interval
        rows = conn.execute(
            f"""SELECT series, ts / ? AS b,
                       SUM(CASE WHEN value >= prev THEN value - prev ELSE value END),
                       SUM(ts - prev_ts)
                FROM (SELECT series, ts, value,
                             LAG(value) OVER w AS prev, LAG(ts) OVER w AS prev_ts
                      FROM samples WHERE series IN ({marks}) AND ts >= ? AND ts < ?
                      WINDOW w AS (PARTITION BY series ORDER BY ts))
                WHERE prev IS NOT NULL AND ts >= ?
                GROUP BY series, b""",
            (step, *ids, lo - lookback, hi, lo),
        ).fetchall()
        out = defaultdict(dict)
        for sid, b, increase, seconds in rows:
            if fn == "rate":
                out[sid][b] = increase / seconds if seconds else None
            elif fn == "per_minute":
                out[sid][b] = increase / seconds * 60 if seconds else None
            else:  # increase, quantile, heatmap
                out[sid][b] = increase
        return out

    def _quantiles(self, chosen, values, spec, first, last) -> list[dict]:
        groups: dict[tuple, dict[float, list[int]]] = defaultdict(lambda: defaultdict(list))
        for sid, labels in chosen.items():
            le = _le(labels)
            if le is not None:
                groups[tuple(labels.get(k, "") for k in spec["by"])][le].append(sid)
        series = []
        for key, by_le in groups.items():
            column = []
            for b in range(first, last + 1):
                buckets = []
                for le, sids in by_le.items():
                    counts = [v for sid in sids if (v := values.get(sid, {}).get(b)) is not None]
                    if counts:
                        buckets.append((le, math.fsum(counts)))
                column.append(histogram_quantile(spec["q"], buckets))
            series.append({"labels": dict(zip(spec["by"], key)), "values": column})
        return series

    def _heatmap(self, chosen, values, first, last) -> list[dict]:
        by_le: dict[float, list[int]] = defaultdict(list)
        names: dict[float, str] = {}
        for sid, labels in chosen.items():
            le = _le(labels)
            if le is not None:
                by_le[le].append(sid)
                names[le] = labels["le"]
        les = sorted(by_le)
        columns = []
        for le in les:
            columns.append([
                math.fsum(v for sid in by_le[le] if (v := values.get(sid, {}).get(b)) is not None)
                if any(values.get(sid, {}).get(b) is not None for sid in by_le[le]) else None
                for b in range(first, last + 1)
            ])
        series = []
        for i, le in enumerate(les):
            below = columns[i - 1] if i else None
            column = [
                None if c is None else max(0.0, c - (below[j] or 0.0) if below else c)
                for j, c in enumerate(columns[i])
            ]
            series.append({"labels": {"le": names[le]}, "values": column})
        return series

    # -- dashboards ------------------------------------------------------

    def dashboards(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT id, name, spec, updated FROM dashboards ORDER BY name COLLATE NOCASE, id").fetchall()
        return [
            {"id": i, "name": n, "panels": len(json.loads(s).get("panels", [])), "updated": u}
            for i, n, s, u in rows
        ]

    def dashboard(self, dashboard_id: int) -> dict:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id, name, spec, created, updated FROM dashboards WHERE id = ?", (dashboard_id,)
            ).fetchone()
        if row is None:
            raise NotFound("no such dashboard")
        i, n, s, c, u = row
        return {"id": i, "name": n, "spec": json.loads(s), "created": c, "updated": u}

    def create_dashboard(self, body: dict) -> dict:
        name, spec = _check_dashboard(body)
        now = time.time()
        with self._write_lock, closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO dashboards (name, spec, created, updated) VALUES (?, ?, ?, ?)",
                (name, json.dumps(spec), now, now),
            )
            new_id = cur.lastrowid
        return self.dashboard(new_id)

    def update_dashboard(self, dashboard_id: int, body: dict) -> dict:
        name, spec = _check_dashboard(body)
        with self._write_lock, closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "UPDATE dashboards SET name = ?, spec = ?, updated = ? WHERE id = ?",
                (name, json.dumps(spec), time.time(), dashboard_id),
            )
        if cur.rowcount == 0:
            raise NotFound("no such dashboard")
        return self.dashboard(dashboard_id)

    def delete_dashboard(self, dashboard_id: int) -> None:
        with self._write_lock, closing(self._connect()) as conn, conn:
            cur = conn.execute("DELETE FROM dashboards WHERE id = ?", (dashboard_id,))
        if cur.rowcount == 0:
            raise NotFound("no such dashboard")


# -- validation ----------------------------------------------------------


def _check_query(spec) -> dict:
    if not isinstance(spec, dict):
        raise QueryError("the query must be a JSON object")
    metric = spec.get("metric")
    if not isinstance(metric, str) or not metric:
        raise QueryError("metric is required")
    match = spec.get("match") or {}
    if not isinstance(match, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in match.items()):
        raise QueryError("match must map label names to values")
    fn = spec.get("fn") or "raw"
    if fn not in FUNCTIONS:
        raise QueryError(f"fn must be one of {', '.join(FUNCTIONS)}")
    agg = spec.get("agg") or "none"
    if agg not in AGGREGATIONS:
        raise QueryError(f"agg must be one of {', '.join(AGGREGATIONS)}")
    by = spec.get("by") or []
    if not isinstance(by, list) or not all(isinstance(k, str) for k in by):
        raise QueryError("by must be a list of label names")
    q = spec.get("q", 0.95)
    if fn == "quantile" and (isinstance(q, bool) or not isinstance(q, (int, float)) or not 0 <= q <= 1):
        raise QueryError("q must be between 0 and 1")
    return {"metric": metric, "match": match, "fn": fn, "agg": agg, "by": by, "q": float(q)}


def _int_in(panel: dict, key: str, lo: int, hi: int) -> int:
    value = panel.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise QueryError(f"panel {key} must be a whole number from {lo} to {hi}")
    return value


def _check_dashboard(body) -> tuple[str, dict]:
    if not isinstance(body, dict):
        raise QueryError("expected a JSON object")
    name = body.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise QueryError("name must be 1 to 200 characters")
    spec = body.get("spec", {"range": 21600, "panels": []})
    if not isinstance(spec, dict):
        raise QueryError("spec must be a JSON object")
    if len(json.dumps(spec)) > MAX_SPEC_BYTES:
        raise QueryError("the dashboard is too large")
    rng = spec.get("range", 21600)
    if isinstance(rng, bool) or not isinstance(rng, (int, float)) or not 60 <= rng <= 366 * 86400:
        raise QueryError("range must be a number of seconds")
    panels = spec.get("panels", [])
    if not isinstance(panels, list) or len(panels) > MAX_PANELS:
        raise QueryError(f"panels must be a list of at most {MAX_PANELS}")
    for panel in panels:
        if not isinstance(panel, dict):
            raise QueryError("each panel must be a JSON object")
        if panel.get("type") not in PANEL_TYPES:
            raise QueryError(f"panel type must be one of {', '.join(PANEL_TYPES)}")
        _int_in(panel, "w", 1, GRID_COLUMNS)
        _int_in(panel, "x", 0, GRID_COLUMNS - panel["w"])
        _int_in(panel, "h", 2, 40)
        _int_in(panel, "y", 0, 10_000)
        if not isinstance(panel.get("title", ""), str):
            raise QueryError("panel title must be text")
        if panel.get("query") is not None:
            _check_query(panel["query"])
    return name.strip(), {**spec, "range": rng, "panels": panels}


# -- scraping ------------------------------------------------------------


class Scraper:
    """Fetches ``url`` every ``db.interval`` seconds and stores what it finds."""

    PRUNE_EVERY = 3600.0

    def __init__(self, db: MetricDB, url: str, host_header: str = "localhost"):
        self.db = db
        self.url = url
        self.host_header = host_header
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="scraper", daemon=True)
        self._failing = False
        self.last_error: str | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def scrape(self, ts: int | None = None) -> int:
        """One scrape, stored at ``ts`` (default: the current interval's start)."""
        if ts is None:
            ts = int(time.time() // self.db.interval * self.db.interval)
        request = urllib.request.Request(self.url, headers={"Host": self.host_header})
        with urllib.request.urlopen(request, timeout=max(5, self.db.interval / 2)) as response:
            text = response.read().decode("utf-8", "replace")
        return self.db.ingest(ts, exposition.parse(text))

    def _loop(self) -> None:
        interval = self.db.interval
        next_prune = 0.0
        tick = time.time() // interval * interval
        while not self._stop.is_set():
            try:
                self.scrape(int(tick))
                if self._failing:
                    print("corral: scraping metrics works again", file=sys.stderr, flush=True)
                self._failing, self.last_error = False, None
            except Exception as exc:  # keep scraping whatever went wrong
                self.last_error = f"{type(exc).__name__}: {exc}"
                if not self._failing:  # once per streak of failures, not every interval
                    print(f"corral: cannot scrape {self.url}: {self.last_error}", file=sys.stderr, flush=True)
                self._failing = True
            now = time.time()
            if now >= next_prune:
                try:
                    self.db.prune(now)
                except sqlite3.Error as exc:
                    print(f"corral: cannot prune old metrics: {exc}", file=sys.stderr, flush=True)
                next_prune = now + self.PRUNE_EVERY
            tick = max(tick + interval, now // interval * interval)
            if tick <= now:
                tick += interval
            self._stop.wait(tick - time.time())


def scrape_url(bind_host: str, port: int) -> str:
    """Where to reach this server's /metrics from the same machine."""
    host = {"0.0.0.0": "127.0.0.1", "::": "::1", "": "127.0.0.1"}.get(bind_host, bind_host)
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}/metrics"
