import re
import threading
import urllib.request

from corral import prometheus
from corral.monitor import Monitor
from corral.web import make_server

from test_supervisor import harness, script, wait_for  # noqa: F401  (fixtures)

SAMPLE = re.compile(r'^([a-z_]+)(?:\{(.*)\})? (-?[0-9.e+-]+)$')


def parse(text):
    """{(metric, (("label", "value"), ...)): number}, checking the format on the way."""
    assert text.endswith("\n")
    samples, typed = {}, set()
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            typed.add(line.split()[2])
        if line.startswith("#"):
            continue
        name, labels, value = SAMPLE.match(line).groups()
        assert name in typed, f"{name} has no TYPE line before its samples"
        key = (name, tuple(sorted(re.findall(r'([a-z_]+)="((?:[^"\\]|\\.)*)"', labels or ""))))
        assert key not in samples
        samples[key] = float(value)
    return samples


def value(samples, metric, **labels):
    return samples.get((f"corral_{metric}", tuple(sorted(labels.items()))))


def test_label_escaping():
    assert prometheus._labels({"script": 'a"b\\c\nd'}) == '{script="a\\"b\\\\c\\nd"}'


def test_metrics_endpoint(tmp_path, harness):  # noqa: F811
    svc = script(tmp_path, "svc", "import sys, time\nprint('a')\nprint('b')\nprint('c', file=sys.stderr)\ntime.sleep(60)\n")
    bad = script(tmp_path, "bad", "raise SystemExit(3)\n", restart_delay=3600.0)
    supervisor, store = harness(svc, bad)
    monitor = Monitor(supervisor, store, interval=3600, retention_days=7)  # sampled by hand
    server = make_server("127.0.0.1", 0, supervisor, store, monitor)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    wait_for(lambda: supervisor.scripts["svc"].counters()["lines"] == {"stdout": 2, "stderr": 1})
    wait_for(lambda: supervisor.scripts["bad"].status()["state"] == "backoff")
    monitor.sample()

    with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/metrics", timeout=5) as response:
        assert response.headers["Content-Type"] == prometheus.CONTENT_TYPE
        m = parse(response.read().decode())
    server.shutdown()

    assert value(m, "script_state", script="svc", state="running") == 1
    assert value(m, "script_state", script="svc", state="stopped") == 0
    assert value(m, "script_up", script="svc") == 1 and value(m, "script_up", script="bad") == 0
    assert value(m, "script_enabled", script="svc") == 1
    assert value(m, "script_runs_total", script="svc") == 1
    assert value(m, "script_run_failures_total", script="svc") == 0
    assert value(m, "script_log_lines_total", script="svc", stream="stdout") == 2
    assert value(m, "script_log_lines_total", script="svc", stream="stderr") == 1
    assert value(m, "script_last_exit_code", script="svc") is None  # has not exited yet
    assert value(m, "script_resident_memory_bytes", script="svc") > 1_000_000
    assert value(m, "script_threads", script="svc") >= 1 and value(m, "script_open_fds", script="svc") >= 3
    assert value(m, "script_cpu_seconds_total", script="svc") >= 0

    assert value(m, "script_state", script="bad", state="backoff") == 1
    assert value(m, "script_run_failures_total", script="bad") == 1
    assert value(m, "script_last_exit_code", script="bad") == 3
    assert value(m, "script_next_run_time_seconds", script="bad") > value(m, "script_last_exit_time_seconds", script="bad")
    assert value(m, "script_resident_memory_bytes", script="bad") is None  # not running
    assert value(m, "script_cpu_seconds_total", script="bad") == 0


def test_requested_stop_is_not_a_failure(tmp_path, harness):  # noqa: F811
    supervisor, _ = harness(script(tmp_path, "svc", "import time\ntime.sleep(60)\n"))
    svc = supervisor.scripts["svc"]
    wait_for(lambda: svc.status()["state"] == "running")
    svc.stop()
    wait_for(lambda: svc.status()["state"] == "stopped")
    m = parse(prometheus.render(supervisor))
    assert value(m, "script_run_failures_total", script="svc") == 0
    assert value(m, "script_enabled", script="svc") == 0
    assert value(m, "script_cpu_seconds_total", script="svc") is None  # no monitor
