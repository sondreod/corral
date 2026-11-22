import pytest

from corral import store as store_module
from corral.store import LogStore


@pytest.fixture
def store(tmp_path):
    s = LogStore(tmp_path / "logs.sqlite3", max_lines_per_process=50)
    yield s
    s.close()


def lines(rows):
    return [r["line"] for r in rows]


def test_query_filters(store):
    store.append("a", "stdout", "hello world")
    store.append("a", "stderr", "Traceback: boom")
    store.append("a", "runner", "exited with code 1")
    store.append("b", "stdout", "other process")
    store.flush()

    assert lines(store.query("a")) == ["hello world", "Traceback: boom", "exited with code 1"]
    assert lines(store.query("a", text="BOOM")) == ["Traceback: boom"]
    assert lines(store.query("a", streams=("stdout", "runner"))) == ["hello world", "exited with code 1"]
    assert lines(store.query("b")) == ["other process"]
    assert store.query("a", text="nothing") == []


def test_filter_is_literal(store):
    store.append("a", "stdout", "100% done")
    store.append("a", "stdout", "1000 done")
    store.append("a", "stdout", "snake_case")
    store.append("a", "stdout", "snakeXcase")
    store.flush()
    assert lines(store.query("a", text="100%")) == ["100% done"]
    assert lines(store.query("a", text="e_c")) == ["snake_case"]


def test_paging(store):
    for n in range(10):
        store.append("a", "stdout", f"line {n}")
    store.flush()

    newest = store.query("a", limit=3)
    assert lines(newest) == ["line 7", "line 8", "line 9"]
    older = store.query("a", before_id=newest[0]["id"], limit=3)
    assert lines(older) == ["line 4", "line 5", "line 6"]
    following = store.query("a", after_id=older[0]["id"], limit=2)
    assert lines(following) == ["line 5", "line 6"]


def test_retention(store, monkeypatch):
    monkeypatch.setattr(store_module, "_PRUNE_EVERY", 100)
    for n in range(120):
        store.append("a", "stdout", f"line {n}")
    store.append("b", "stdout", "kept")
    store.flush()
    kept = lines(store.query("a", limit=1000))
    assert len(kept) <= 50 + 20 and kept[-1] == "line 119"
    assert lines(store.query("b")) == ["kept"]


def test_persists(tmp_path):
    first = LogStore(tmp_path / "db")
    first.append("a", "stdout", "survives")
    first.close()
    second = LogStore(tmp_path / "db")
    assert lines(second.query("a")) == ["survives"]
    second.close()


def test_metrics_are_bucketed(store):
    for ts, cpu, rss in [(100, 10.0, 1000), (105, 30.0, 3000), (130, 50.0, 2000)]:
        store.add_sample("a", ts, cpu=cpu, rss=rss, threads=2, fds=5, out_lines=3, err_lines=1)
    store.add_sample("a", 190, out_lines=4)  # printed while not running
    store.add_sample("b", 100, cpu=99.0, rss=1, threads=1, fds=1)
    store.flush()

    first, second, third = store.metrics("a", since=60, bucket=60)
    assert first == {
        "t": 60, "cpu": 20.0, "cpu_max": 30.0, "rss": 2000, "rss_max": 3000,
        "threads": 2, "fds": 5, "out_lines": 6, "err_lines": 2,
    }
    assert (second["t"], second["cpu"], second["out_lines"]) == (120, 50.0, 3)
    assert (third["t"], third["cpu"], third["rss"], third["out_lines"]) == (180, None, None, 4)
    assert store.metrics("a", since=120, bucket=60)[0]["t"] == 120


def test_run_history(store):
    store.run_started("a", 100)
    store.run_finished("a", 100, 160, 0, None)
    store.run_started("a", 200)
    store.run_finished("a", 200, 201, 3, None)
    store.run_started("a", 300)
    store.run_finished("a", 300, 310, -15, "stopped")
    store.run_started("a", 400)
    store.run_finished("a", 400, 400, None, "uv not found")
    store.run_started("a", 500)  # still running
    store.run_started("b", 100)
    store.flush()

    runs = store.runs("a")
    assert [r["started"] for r in runs] == [500, 400, 300, 200, 100]
    assert runs[0]["ended"] is None
    assert runs[1] == {"started": 400, "ended": 400, "exit_code": None, "note": "uv not found"}
    assert len(store.runs("a", limit=2)) == 2
    assert store.run_summary("a", since=0) == {"total": 5, "failed": 2}
    assert store.run_summary("a", since=250) == {"total": 3, "failed": 1}
    assert store.run_summary("nobody", since=0) == {"total": 0, "failed": 0}


def test_prune_history(store):
    store.add_sample("a", 100, cpu=1.0)
    store.add_sample("a", 900, cpu=1.0)
    store.run_started("a", 50)
    store.run_finished("a", 50, 60, 0, None)
    store.run_started("a", 70)  # old but still running: kept
    store.prune_history(500)
    store.flush()
    assert [p["t"] for p in store.metrics("a", 0, 1)] == [900]
    assert [r["started"] for r in store.runs("a")] == [70]


def test_open_runs_are_closed_on_startup(tmp_path):
    first = LogStore(tmp_path / "db")
    first.run_started("a", 100)
    first.add_sample("a", 150, cpu=1.0)
    first.run_started("b", 100)
    first.close()
    second = LogStore(tmp_path / "db")
    assert second.runs("a") == [{"started": 100, "ended": 150, "exit_code": None, "note": "interrupted"}]
    assert second.runs("b")[0]["ended"] == 100
    assert second.run_summary("a", 0) == {"total": 1, "failed": 0}
    second.close()


def test_migrates_the_script_column(tmp_path):
    import sqlite3
    from contextlib import closing

    path = tmp_path / "logs.sqlite3"
    with closing(sqlite3.connect(path)) as conn:  # the schema from before the rename
        conn.executescript(
            "CREATE TABLE logs (id INTEGER PRIMARY KEY AUTOINCREMENT, script TEXT NOT NULL, ts REAL NOT NULL,"
            " stream TEXT NOT NULL, line TEXT NOT NULL);"
            "CREATE INDEX logs_script_id ON logs (script, id);"
            "CREATE TABLE runs (script TEXT NOT NULL, started REAL NOT NULL, ended REAL, exit_code INTEGER,"
            " note TEXT, PRIMARY KEY (script, started));"
            "CREATE TABLE samples (script TEXT NOT NULL, ts REAL NOT NULL, cpu REAL, rss INTEGER, threads INTEGER,"
            " fds INTEGER, out_lines INTEGER NOT NULL, err_lines INTEGER NOT NULL);"
            "CREATE INDEX samples_script_ts ON samples (script, ts);"
            "INSERT INTO logs (script, ts, stream, line) VALUES ('shop.web', 1, 'stdout', 'kept');"
            "INSERT INTO runs (script, started, ended, exit_code) VALUES ('shop.web', 1, 2, 0);"
        )
    s = LogStore(path)
    try:
        assert lines(s.query("shop.web")) == ["kept"]
        assert [r["exit_code"] for r in s.runs("shop.web")] == [0]
        s.append("shop.web", "stdout", "new")
        s.flush()
        assert lines(s.query("shop.web")) == ["kept", "new"]
    finally:
        s.close()
    with closing(sqlite3.connect(path)) as conn:
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "logs_script_id" not in indexes and "logs_process_id" in indexes
