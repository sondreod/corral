"""End-to-end tests of the supervisor and web API.

Processes are small Python scripts run with the current interpreter, so the
tests need neither uv nor the network.
"""

import json
import os
import shlex
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from dataclasses import replace

from corral.config import Config, ScriptConfig
from corral.store import LogStore
from corral.supervisor import Supervisor
from corral.web import make_server


@pytest.fixture
def harness(tmp_path):
    created = []

    def make(*scripts):
        store = LogStore(tmp_path / "logs.sqlite3")
        supervisor = Supervisor(Config(scripts=scripts), store)
        created.append((supervisor, store))
        supervisor.start()
        return supervisor, store

    yield make
    for supervisor, store in created:
        supervisor.shutdown()
        store.close()


def script(tmp_path, name, body, args=(), **kwargs):
    """A process running ``body`` as a Python script, as a Procfile line would."""
    path = tmp_path / f"{name}.py"
    path.write_text(body)
    command = shlex.join([sys.executable, str(path), *args])
    return ScriptConfig(name=name, command=command, cwd=tmp_path, **kwargs)


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
    assert "still running after 0.3s, sending SIGKILL" in logged(store, "stubborn", "runner")

    def child_gone():
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return True
        # A zombie still answers to kill(0); check its state.
        with open(f"/proc/{child_pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] == "Z"

    wait_for(child_gone)


def test_disabled_script_does_not_start(tmp_path, harness):
    supervisor, _ = harness(script(tmp_path, "off", "print('x')\n", enabled=False))
    time.sleep(0.3)
    assert supervisor.scripts["off"].status()["runs"] == 0
    assert supervisor.scripts["off"].status()["state"] == "stopped"


def test_unknown_command_is_reported_with_backoff(tmp_path, harness):
    cfg = ScriptConfig(name="svc", command="no-such-command-here --flag", cwd=tmp_path, restart_delay=0.1)
    supervisor, store = harness(cfg)
    svc = supervisor.scripts["svc"]
    wait_for(lambda: svc.status()["runs"] >= 2)
    assert "exited with code 127" in logged(store, "svc", "runner")  # the shell's "not found"
    assert any("not found" in l for l in logged(store, "svc", "stderr"))
    assert svc.status()["exit_code"] == 127


def test_shell_command_environment_and_cwd(tmp_path, harness):
    (tmp_path / "bin").mkdir()
    tool = tmp_path / "bin" / "greet"
    tool.write_text("#!/bin/sh\necho \"hello $1\"\n")
    tool.chmod(0o755)
    cfg = ScriptConfig(
        name="app.web", app="app", process="web", cwd=tmp_path, path=(str(tmp_path / "bin"),),
        command='greet "$WHO" && pwd && echo "metrics=$CORRAL_METRICS" >&2 && sleep 60',
        env={"WHO": "world", "CORRAL_METRICS": "0"},
    )
    supervisor, store = harness(cfg)
    wait_for(lambda: len(logged(store, "app.web", "stdout")) == 2)
    assert logged(store, "app.web", "stdout") == ["hello world", str(tmp_path)]
    assert logged(store, "app.web", "stderr") == ["metrics=1"]  # always set by corral
    status = supervisor.scripts["app.web"].status()
    assert (status["app"], status["process"], status["command"]) == ("app", "web", cfg.command)


def test_reload(tmp_path, harness):
    svc = script(tmp_path, "svc", "import os, time\nprint('v', os.environ.get('V'), flush=True)\ntime.sleep(60)\n")
    gone = script(tmp_path, "gone", "import time\ntime.sleep(60)\n")
    supervisor, store = harness(svc, gone)
    wait_for(lambda: logged(store, "svc", "stdout") == ["v None"])
    wait_for(lambda: supervisor.scripts["gone"].status()["state"] == "running")
    first_pid = supervisor.scripts["svc"].status()["pid"]

    new = script(tmp_path, "new", "import time\nprint('new up')\ntime.sleep(60)\n")
    result = supervisor.reload(Config(scripts=(replace(svc, env={"V": "2"}), new)))
    assert result == {"added": ["new"], "changed": ["svc"], "removed": ["gone"]}

    assert "gone" not in supervisor.scripts
    wait_for(lambda: "killed by SIGTERM" in logged(store, "gone", "runner"))
    wait_for(lambda: logged(store, "svc", "stdout") == ["v None", "v 2"])  # restarted with the new env
    assert supervisor.scripts["svc"].status()["pid"] != first_pid
    assert "configuration changed, restarting" in logged(store, "svc", "runner")
    wait_for(lambda: "new up" in logged(store, "new"))

    unchanged = supervisor.scripts["svc"]
    assert supervisor.reload(supervisor.config) == {"added": [], "changed": [], "removed": []}
    assert supervisor.scripts["svc"] is unchanged


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
        assert status == 200 and b"<title>corral</title>" in body

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
        status, body = call("/api/scripts/svc/stop", "POST", {"X-Corral": "1"})
        assert status == 200 and json.loads(body)["enabled"] is False
        wait_for(lambda: supervisor.scripts["svc"].status()["state"] == "stopped")
    finally:
        server.shutdown()
        server.server_close()
