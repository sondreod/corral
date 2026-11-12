"""End-to-end tests of the supervisor and web API.

A stand-in for uv is used so the tests need neither uv nor the network: it
accepts ``run --quiet --script <path> [args...]`` and runs the script with the
current interpreter.
"""

import json
import os
import stat
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from scriptrunner.config import Config, ScriptConfig
from scriptrunner.store import LogStore
from scriptrunner.supervisor import Supervisor
from scriptrunner.web import make_server


@pytest.fixture
def fake_uv(tmp_path):
    path = tmp_path / "fake-uv"
    path.write_text(f'#!/bin/sh\n[ "$1 $2 $3" = "run --quiet --script" ] || exit 99\nshift 3\nexec {sys.executable} "$@"\n')
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture
def harness(tmp_path, fake_uv):
    created = []

    def make(*scripts, uv=fake_uv):
        store = LogStore(tmp_path / "logs.sqlite3")
        supervisor = Supervisor(Config(uv=uv, scripts=scripts), store)
        created.append((supervisor, store))
        supervisor.start()
        return supervisor, store

    yield make
    for supervisor, store in created:
        supervisor.shutdown()
        store.close()


def script(tmp_path, name, body, **kwargs):
    path = tmp_path / f"{name}.py"
    path.write_text(body)
    return ScriptConfig(name=name, path=path, **kwargs)


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def logged(store, name, stream=None):
    store.flush()
    streams = (stream,) if stream else None
    return [r["line"] for r in store.query(name, streams=streams, limit=5000)]


def test_service_captures_output_and_restarts(tmp_path, harness):
    cfg = script(
        tmp_path,
        "svc",
        "import sys\nprint('to stdout', sys.argv[1])\nprint('to stderr', file=sys.stderr)\nsys.exit(3)\n",
        args=("arg",),
        restart_delay=0.1,
    )
    supervisor, store = harness(cfg)
    svc = supervisor.scripts["svc"]
    wait_for(lambda: svc.status()["runs"] >= 2)
    wait_for(lambda: svc.status()["exit_code"] == 3)
    assert "to stdout arg" in logged(store, "svc", "stdout")
    assert "to stderr" in logged(store, "svc", "stderr")
    assert "exited with code 3" in logged(store, "svc", "runner")
    finished = [r for r in store.runs("svc") if r["ended"] is not None]
    assert finished and all(r["exit_code"] == 3 and r["note"] is None for r in finished)
    assert store.run_summary("svc", 0)["failed"] == len(finished)


def test_stop_start_restart(tmp_path, harness):
    cfg = script(tmp_path, "svc", "import time\nprint('up')\ntime.sleep(60)\n")
    supervisor, store = harness(cfg)
    svc = supervisor.scripts["svc"]
    wait_for(lambda: "up" in logged(store, "svc"))
    first_pid = svc.status()["pid"]
    assert svc.status()["state"] == "running" and first_pid

    svc.stop()
    wait_for(lambda: svc.status()["state"] == "stopped")
    assert svc.status()["pid"] is None and not svc.status()["enabled"]
    assert "killed by SIGTERM" in logged(store, "svc", "runner")
    assert store.runs("svc")[0]["note"] == "stopped"
    assert store.run_summary("svc", 0) == {"total": 1, "failed": 0}
    time.sleep(0.3)
    assert svc.status()["runs"] == 1  # stays stopped

    svc.start()
    wait_for(lambda: svc.status()["state"] == "running" and svc.status()["pid"])
    second_pid = svc.status()["pid"]
    assert second_pid != first_pid

    svc.restart()
    wait_for(lambda: svc.status()["runs"] == 3 and svc.status()["pid"] not in (None, second_pid))


def test_sigkill_after_stop_timeout_and_orphans_cleaned(tmp_path, harness):
    body = (
        "import signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print('child', child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    supervisor, store = harness(script(tmp_path, "stubborn", body, stop_timeout=0.3))
    svc = supervisor.scripts["stubborn"]
    line = wait_for(lambda: next((l for l in logged(store, "stubborn") if l.startswith("child ")), None))
    child_pid = int(line.split()[1])

    svc.stop()
    wait_for(lambda: svc.status()["state"] == "stopped")
    assert "killed by SIGKILL" in logged(store, "stubborn", "runner")

    def child_gone():
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return True
        # A zombie still answers to kill(0); check its state.
        with open(f"/proc/{child_pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] == "Z"

    wait_for(child_gone)


def test_schedule_every(tmp_path, harness):
    cfg = script(tmp_path, "job", "print('ran')\n", mode="schedule", every=0.3)
    supervisor, store = harness(cfg)
    job = supervisor.scripts["job"]
    wait_for(lambda: job.status()["runs"] >= 3)
    status = wait_for(lambda: (s := job.status())["state"] == "waiting" and s)
    assert status["exit_code"] == 0 and status["next_run"] > time.time() - 1

    job.stop()
    wait_for(lambda: job.status()["state"] == "stopped")
    runs = job.status()["runs"]
    time.sleep(0.6)
    assert job.status()["runs"] == runs

    job.restart()  # "run now" works while the schedule is disabled, and leaves it disabled
    wait_for(lambda: job.status()["runs"] == runs + 1)
    wait_for(lambda: job.status()["state"] == "stopped")


def test_schedule_cron_waits(tmp_path, harness):
    from scriptrunner.schedule import Cron

    cfg = script(tmp_path, "job", "print('ran')\n", mode="schedule", cron=Cron("0 0 1 1 *"))
    supervisor, _ = harness(cfg)
    job = supervisor.scripts["job"]
    status = wait_for(lambda: (s := job.status())["state"] == "waiting" and s)
    assert status["runs"] == 0 and status["next_run"] > time.time()


def test_disabled_script_does_not_start(tmp_path, harness):
    supervisor, _ = harness(script(tmp_path, "off", "print('x')\n", enabled=False))
    time.sleep(0.3)
    assert supervisor.scripts["off"].status()["runs"] == 0
    assert supervisor.scripts["off"].status()["state"] == "stopped"


def test_missing_uv_is_reported_with_backoff(tmp_path, harness):
    cfg = script(tmp_path, "svc", "print('x')\n", restart_delay=0.1)
    supervisor, store = harness(cfg, uv=str(tmp_path / "no-such-uv"))
    svc = supervisor.scripts["svc"]
    wait_for(lambda: svc.status()["runs"] >= 2)
    assert any("failed to start" in l for l in logged(store, "svc", "runner"))
    assert svc.status()["exit_code"] is None


def test_web_api(tmp_path, harness):
    cfg = script(tmp_path, "svc", "import time\nprint('needle in haystack')\nprint('hay')\ntime.sleep(60)\n")
    supervisor, store = harness(cfg)
    server = make_server("127.0.0.1", 0, supervisor, store)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(path, method="GET", headers=None):
        request = urllib.request.Request(base + path, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    try:
        wait_for(lambda: "hay" in logged(store, "svc"))

        status, body = call("/")
        assert status == 200 and b"<title>scriptrunner</title>" in body

        status, body = call("/api/scripts")
        (info,) = json.loads(body)["scripts"]
        assert status == 200 and info["name"] == "svc" and info["state"] == "running"

        status, body = call("/api/scripts/svc/logs?q=NEEDLE&streams=stdout")
        assert [l["line"] for l in json.loads(body)["lines"]] == ["needle in haystack"]

        assert info["usage"] is None  # no monitor attached to this server

        store.add_sample("svc", time.time(), cpu=12.5, rss=2048, threads=1, fds=4, out_lines=2)
        store.flush()
        status, body = call("/api/scripts/svc/metrics?range=600&points=10")
        metrics = json.loads(body)
        assert status == 200 and metrics["bucket"] == 60 and metrics["runs"] == {"total": 1, "failed": 0}
        assert [(p["cpu"], p["rss"], p["out_lines"]) for p in metrics["points"]] == [(12.5, 2048, 2)]
        assert call("/api/scripts/svc/metrics?range=x")[0] == 400

        status, body = call("/api/scripts/svc/runs")
        (run,) = json.loads(body)["runs"]
        assert status == 200 and run["ended"] is None and run["exit_code"] is None

        assert call("/api/scripts/svc/logs?streams=bogus")[0] == 400
        assert call("/api/scripts/svc/logs?limit=x")[0] == 400
        assert call("/api/scripts/nope/logs")[0] == 404
        assert call("/api/other")[0] == 404

        assert call("/api/scripts/svc/stop", "POST")[0] == 403  # no CSRF header
        assert supervisor.scripts["svc"].status()["enabled"]
        status, body = call("/api/scripts/svc/stop", "POST", {"X-Scriptrunner": "1"})
        assert status == 200 and json.loads(body)["enabled"] is False
        wait_for(lambda: supervisor.scripts["svc"].status()["state"] == "stopped")
    finally:
        server.shutdown()
        server.server_close()


def test_project_problem_is_reported(tmp_path, harness):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "demo"\n[project.scripts]\na = "demo:a"\nb = "demo:b"\n'
    )
    cfg = ScriptConfig(name="app", project=project, entrypoint="missing", restart_delay=0.1)
    supervisor, store = harness(cfg)
    app = supervisor.scripts["app"]
    wait_for(lambda: app.status()["runs"] >= 2)
    assert any("cannot start" in l and "declared: a, b" in l for l in logged(store, "app", "runner"))
    assert app.status()["exit_code"] is None and app.status()["entrypoint"] == "missing"


def test_project_command_line(tmp_path, fake_uv):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "pyproject.toml").write_text('[project]\nname = "demo"\n[project.scripts]\nserve = "demo:a"\n')
    cfg = ScriptConfig(name="app", project=project, args=("--port", "1"))
    store = LogStore(tmp_path / "db")
    try:
        from scriptrunner.supervisor import Script

        assert Script(cfg, store, "uv")._command() == [
            "uv", "run", "--quiet", "--project", str(project), "serve", "--port", "1",
        ]
    finally:
        store.close()
