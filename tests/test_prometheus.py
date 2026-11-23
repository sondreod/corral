import re
import threading
import urllib.request

from corral import prometheus
from corral.monitor import Monitor
from corral.web import make_server

from test_supervisor import harness, process, wait_for  # noqa: F401  (fixtures)

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
    assert prometheus._labels({"process": 'a"b\\c\nd'}) == '{process="a\\"b\\\\c\\nd"}'


def test_metrics_endpoint(tmp_path, harness):  # noqa: F811
    svc = process(tmp_path, "svc", "import sys, time\nprint('a')\nprint('b')\nprint('c', file=sys.stderr)\ntime.sleep(60)\n")
    bad = process(tmp_path, "bad", "raise SystemExit(3)\n", restart_delay=3600.0)
    supervisor, store = harness(svc, bad)
    monitor = Monitor(supervisor, store, interval=3600, retention_days=7)  # sampled by hand
    server = make_server("127.0.0.1", 0, supervisor, store, monitor)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    wait_for(lambda: supervisor.processes["svc"].counters()["lines"] == {"stdout": 2, "stderr": 1})
    wait_for(lambda: supervisor.processes["bad"].status()["state"] == "backoff")
    monitor.sample()

    with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/metrics", timeout=5) as response:
        assert response.headers["Content-Type"] == prometheus.CONTENT_TYPE
        m = parse(response.read().decode())
    server.shutdown()

    assert value(m, "process_state", process="svc", state="running") == 1
    assert value(m, "process_state", process="svc", state="stopped") == 0
    assert value(m, "process_up", process="svc") == 1 and value(m, "process_up", process="bad") == 0
    assert value(m, "process_enabled", process="svc") == 1
    assert value(m, "process_runs_total", process="svc") == 1
    assert value(m, "process_run_failures_total", process="svc") == 0
    assert value(m, "process_log_lines_total", process="svc", stream="stdout") == 2
    assert value(m, "process_log_lines_total", process="svc", stream="stderr") == 1
    assert value(m, "process_last_exit_code", process="svc") is None  # has not exited yet
    assert value(m, "process_resident_memory_bytes", process="svc") > 1_000_000
    assert value(m, "process_threads", process="svc") >= 1 and value(m, "process_open_fds", process="svc") >= 3
    assert value(m, "process_cpu_seconds_total", process="svc") >= 0

    assert value(m, "process_state", process="bad", state="backoff") == 1
    assert value(m, "process_run_failures_total", process="bad") == 1
    assert value(m, "process_last_exit_code", process="bad") == 3
    assert value(m, "process_next_run_time_seconds", process="bad") > value(m, "process_last_exit_time_seconds", process="bad")
    assert value(m, "process_resident_memory_bytes", process="bad") is None  # not running
    assert value(m, "process_cpu_seconds_total", process="bad") == 0


def test_requested_stop_is_not_a_failure(tmp_path, harness):  # noqa: F811
    supervisor, _ = harness(process(tmp_path, "svc", "import time\ntime.sleep(60)\n"))
    svc = supervisor.processes["svc"]
    wait_for(lambda: svc.status()["state"] == "running")
    svc.stop()
    wait_for(lambda: svc.status()["state"] == "stopped")
    m = parse(prometheus.render(supervisor))
    assert value(m, "process_run_failures_total", process="svc") == 0
    assert value(m, "process_enabled", process="svc") == 0
    assert value(m, "process_cpu_seconds_total", process="svc") is None  # no monitor


def test_loki_counters(tmp_path, harness):  # noqa: F811
    from collections import Counter
    from types import SimpleNamespace

    supervisor, _ = harness()
    m = parse(prometheus.render(supervisor))
    assert value(m, "loki_lines_sent_total") is None  # no [loki]: no loki metrics
    loki = SimpleNamespace(sent=7, dropped=Counter(buffer_full=2), failures=1, buffered=lambda: 3)
    m = parse(prometheus.render(supervisor, loki=loki))
    assert value(m, "loki_lines_sent_total") == 7
    assert value(m, "loki_lines_dropped_total", reason="buffer_full") == 2
    assert value(m, "loki_lines_dropped_total", reason="rejected") == 0
    assert value(m, "loki_push_failures_total") == 1
    assert value(m, "loki_buffered_lines") == 3
