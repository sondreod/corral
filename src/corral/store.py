"""SQLite-backed storage for logs, run history and resource samples.

Lines are queued by the threads reading process output and written in batches
by a single writer thread, so a chatty process costs one transaction per batch
instead of one per line. Readers open their own short-lived connections (WAL
mode lets them read while the writer writes).
"""

from __future__ import annotations

import queue
import sqlite3
import threading
import time
from collections import Counter
from contextlib import closing
from pathlib import Path

STREAMS = ("stdout", "stderr", "runner")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    process TEXT NOT NULL,
    ts      REAL NOT NULL,
    stream  TEXT NOT NULL,
    line    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS logs_process_id ON logs (process, id);

-- One row per start of a process. `ended` is NULL while it runs; `exit_code`
-- is NULL if it could not be started; `note` is 'stopped' when the run was
-- ended on request, or the reason it could not start.
CREATE TABLE IF NOT EXISTS runs (
    process   TEXT NOT NULL,
    started   REAL NOT NULL,
    ended     REAL,
    exit_code INTEGER,
    note      TEXT,
    PRIMARY KEY (process, started)
);

-- Periodic measurements of a process and everything it started, plus the
-- number of lines it printed since the previous sample.
CREATE TABLE IF NOT EXISTS samples (
    process   TEXT NOT NULL,
    ts        REAL NOT NULL,
    cpu       REAL,     -- percent of one core
    rss       INTEGER,  -- bytes
    threads   INTEGER,
    fds       INTEGER,
    out_lines INTEGER NOT NULL,
    err_lines INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_process_ts ON samples (process, ts);

-- Runs left open by a corral that died: close them at the last moment
-- the process was seen alive.
UPDATE runs SET
    ended = COALESCE(
        (SELECT MAX(ts) FROM samples WHERE samples.process = runs.process AND ts >= runs.started),
        started),
    note = 'interrupted'
WHERE ended IS NULL;
"""

_PRUNE_EVERY = 1000  # lines written per process between retention checks
_BATCH_MAX = 5000
_STOP = object()


def _migrate(conn: sqlite3.Connection) -> None:
    """Databases from before the rename have a ``script`` column where
    there is now ``process``."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(logs)")}
    if "script" not in columns:
        return
    with conn:
        conn.execute("DROP INDEX IF EXISTS logs_script_id")
        conn.execute("DROP INDEX IF EXISTS samples_script_ts")
        for table in ("logs", "runs", "samples"):
            conn.execute(f"ALTER TABLE {table} RENAME COLUMN script TO process")


class _Sql(tuple):
    """A (statement, parameters) pair for the writer thread."""


class LogStore:
    def __init__(self, path: str | Path, max_lines_per_process: int = 100_000):
        self.path = str(path)
        self.max_lines = max_lines_per_process
        self._queue: queue.Queue = queue.Queue()
        self._since_prune: Counter[str] = Counter()
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            _migrate(conn)
            conn.executescript(_SCHEMA)
        self._writer = threading.Thread(target=self._write_loop, name="log-writer", daemon=True)
        self._writer.start()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # -- writing ---------------------------------------------------------

    def append(self, process: str, stream: str, line: str, ts: float | None = None) -> None:
        self._queue.put((process, time.time() if ts is None else ts, stream, line))

    def run_started(self, process: str, started: float) -> None:
        self._queue.put(_Sql(("INSERT OR REPLACE INTO runs (process, started) VALUES (?, ?)", (process, started))))

    def run_finished(
        self, process: str, started: float, ended: float, exit_code: int | None, note: str | None
    ) -> None:
        self._queue.put(
            _Sql(
                (
                    "UPDATE runs SET ended = ?, exit_code = ?, note = ? WHERE process = ? AND started = ?",
                    (ended, exit_code, note, process, started),
                )
            )
        )

    def add_sample(
        self,
        process: str,
        ts: float,
        *,
        cpu: float | None = None,
        rss: int | None = None,
        threads: int | None = None,
        fds: int | None = None,
        out_lines: int = 0,
        err_lines: int = 0,
    ) -> None:
        self._queue.put(
            _Sql(
                (
                    "INSERT INTO samples (process, ts, cpu, rss, threads, fds, out_lines, err_lines)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (process, ts, cpu, rss, threads, fds, out_lines, err_lines),
                )
            )
        )

    def prune_history(self, before: float) -> None:
        """Drop samples and finished runs older than ``before``."""
        self._queue.put(_Sql(("DELETE FROM samples WHERE ts < ?", (before,))))
        self._queue.put(_Sql(("DELETE FROM runs WHERE ended < ?", (before,))))

    def flush(self) -> None:
        """Block until everything queued so far is on disk."""
        done = threading.Event()
        self._queue.put(done)
        done.wait()

    def close(self) -> None:
        self._queue.put(_STOP)
        self._writer.join()

    def _write_loop(self) -> None:
        conn = self._connect()
        stop = False
        while not stop:
            items = [self._queue.get()]
            time.sleep(0.05)  # let a burst accumulate into one transaction
            try:
                while len(items) < _BATCH_MAX:
                    items.append(self._queue.get_nowait())
            except queue.Empty:
                pass

            rows = [i for i in items if isinstance(i, tuple) and not isinstance(i, _Sql)]
            stop = any(i is _STOP for i in items)
            try:
                with conn:
                    conn.executemany(
                        "INSERT INTO logs (process, ts, stream, line) VALUES (?, ?, ?, ?)", rows
                    )
                    for item in items:
                        if isinstance(item, _Sql):
                            conn.execute(*item)
                    self._since_prune.update(r[0] for r in rows)
                    for process, count in list(self._since_prune.items()):
                        if count >= _PRUNE_EVERY:
                            self._prune(conn, process)
                            del self._since_prune[process]
            except sqlite3.Error as exc:  # keep running; losing a batch beats dying
                print(f"corral: log write failed: {exc}", flush=True)
            for item in items:
                if isinstance(item, threading.Event):
                    item.set()
        conn.close()

    def _prune(self, conn: sqlite3.Connection, process: str) -> None:
        conn.execute(
            "DELETE FROM logs WHERE process = ?1 AND id <= ("
            " SELECT id FROM logs WHERE process = ?1 ORDER BY id DESC LIMIT 1 OFFSET ?2)",
            (process, self.max_lines),
        )

    # -- reading ---------------------------------------------------------

    def query(
        self,
        process: str,
        *,
        text: str = "",
        streams: tuple[str, ...] | None = None,
        after_id: int | None = None,
        before_id: int | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """Matching lines, oldest first.

        With ``after_id`` the *first* ``limit`` lines after it are returned
        (for following a log); otherwise the *last* ``limit`` lines, optionally
        only those older than ``before_id`` (for paging backwards).
        """
        where = ["process = ?"]
        params: list = [process]
        if text:
            escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("line LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
        if streams is not None:
            where.append(f"stream IN ({','.join('?' * len(streams))})")
            params.extend(streams)
        if after_id is not None:
            where.append("id > ?")
            params.append(after_id)
        if before_id is not None:
            where.append("id < ?")
            params.append(before_id)
        order = "ASC" if after_id is not None else "DESC"
        sql = (
            f"SELECT id, ts, stream, line FROM logs WHERE {' AND '.join(where)}"
            f" ORDER BY id {order} LIMIT ?"
        )
        params.append(limit)
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        if order == "DESC":
            rows.reverse()
        return [{"id": r[0], "ts": r[1], "stream": r[2], "line": r[3]} for r in rows]

    def metrics(self, process: str, since: float, bucket: float) -> list[dict]:
        """Samples from ``since`` on, aggregated into ``bucket``-second steps.

        Steps without samples (the process was not running and printed nothing)
        are absent.
        """
        sql = (
            "SELECT CAST(ts / ?1 AS INTEGER) * ?1 AS t, AVG(cpu), MAX(cpu), AVG(rss), MAX(rss),"
            " MAX(threads), MAX(fds), SUM(out_lines), SUM(err_lines)"
            " FROM samples WHERE process = ?2 AND ts >= ?3 GROUP BY t ORDER BY t"
        )
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, (bucket, process, since)).fetchall()
        keys = ("t", "cpu", "cpu_max", "rss", "rss_max", "threads", "fds", "out_lines", "err_lines")
        return [dict(zip(keys, row)) for row in rows]

    def runs(self, process: str, limit: int = 50) -> list[dict]:
        """The most recent runs, newest first."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT started, ended, exit_code, note FROM runs WHERE process = ?"
                " ORDER BY started DESC LIMIT ?",
                (process, limit),
            ).fetchall()
        return [dict(zip(("started", "ended", "exit_code", "note"), row)) for row in rows]

    def run_summary(self, process: str, since: float) -> dict:
        """How many runs started since ``since``, and how many of them failed
        (could not start, or exited non-zero without being asked to stop)."""
        with closing(self._connect()) as conn:
            total, failed = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(ended IS NOT NULL AND COALESCE(note, '') NOT IN"
                " ('stopped', 'interrupted') AND COALESCE(exit_code, -1) != 0), 0)"
                " FROM runs WHERE process = ? AND started >= ?",
                (process, since),
            ).fetchone()
        return {"total": total, "failed": failed}
