import json

import pytest

from corral import metrics as client
from corral import prometheus
from corral.custom import MAX_SERIES, MetricError, ProcessMetrics

from test_prometheus import parse
from test_supervisor import harness, logged, process, wait_for  # noqa: F401  (fixtures)


def send(metrics, **message):
    metrics.handle(json.dumps(message))


def samples(metrics):
    return {
        (sample, tuple(sorted(labels.items()))): value
        for _, _, _, rows in metrics.families()
        for sample, labels, value in rows
    }


def test_counter_and_gauge():
    m = ProcessMetrics()
    send(m, name="jobs", op="declare", type="counter", help="Jobs done")
    send(m, name="jobs", op="inc")
    send(m, name="jobs", op="inc", value=2.5, labels={"queue": "mail"})
    send(m, name="jobs", op="inc", value=1, labels={"queue": "mail"})
    send(m, name="depth", op="set", value=7)  # undeclared: a gauge
    send(m, name="depth", op="inc", value=-2)
    assert samples(m) == {
        ("jobs_total", ()): 1,
        ("jobs_total", (("queue", "mail"),)): 3.5,
        ("depth", ()): 5,
    }
    assert [(name, kind, help) for name, kind, help, _ in m.families()] == [
        ("jobs_total", "counter", "Jobs done"),
        ("depth", "gauge", ""),
    ]


def test_histogram_and_summary():
    m = ProcessMetrics()
    send(m, name="took", op="declare", type="histogram", buckets=[1, 5])
    for value in (0.5, 1, 3, 100):
        send(m, name="took", op="observe", value=value)
    send(m, name="size", op="observe", type="summary", value=10)
    send(m, name="size", op="observe", value=30)
    assert samples(m) == {
        ("took_bucket", (("le", "1.0"),)): 2,
        ("took_bucket", (("le", "5.0"),)): 3,
        ("took_bucket", (("le", "+Inf"),)): 4,
        ("took_count", ()): 4,
        ("took_sum", ()): 104.5,
        ("size_count", ()): 2,
        ("size_sum", ()): 40,
    }


def test_redeclaring_keeps_values_unless_the_type_changes():
    m = ProcessMetrics()
    send(m, name="jobs_total", op="declare", type="counter")
    send(m, name="jobs_total", op="inc", value=4)
    send(m, name="jobs_total", op="declare", type="counter", help="now with help")  # the next run
    send(m, name="jobs_total", op="inc")
    assert samples(m) == {("jobs_total", ()): 5}
    send(m, name="jobs_total", op="declare", type="gauge")
    assert samples(m) == {}


@pytest.mark.parametrize(
    "text, reason",
    [
        ("nonsense", "JSON"),
        ("[1]", "object"),
        ('{"name": "x", "op": "inc", "colour": 1}', "unknown key"),
        ('{"name": "9x", "op": "inc"}', "name"),
        ('{"name": "corral_x", "op": "inc"}', "reserved"),
        ('{"name": "x", "op": "explode"}', "op must be"),
        ('{"name": "x", "op": "declare"}', "needs a type"),
        ('{"name": "x", "op": "declare", "type": "gauge", "buckets": [1]}', "buckets"),
        ('{"name": "x", "op": "declare", "type": "histogram", "buckets": [2, 1]}', "increasing"),
        ('{"name": "x", "op": "inc", "value": -1}', "only go up"),
        ('{"name": "x", "op": "inc", "value": "1"}', "finite number"),
        ('{"name": "x", "op": "set"}', "finite number"),
        ('{"name": "x", "op": "set", "type": "counter", "value": 1}', "cannot set a counter"),
        ('{"name": "x", "op": "inc", "labels": {"process": "a"}}', "label name"),
        ('{"name": "x", "op": "inc", "labels": {"a": [1]}}', "string or number"),
        ('{"name": "x", "op": "set", "value": 1' + "0" * 400 + "}", "too large"),
        ('{"name": "x", "op": "set", "value": 1e999}', "finite number"),
        ('{"name": "x", "op": "set", "value": NaN}', "finite number"),
        ("[" * 100_000, "nested too deeply"),
    ],
)
def test_invalid_lines(text, reason):
    with pytest.raises(MetricError, match=reason):
        ProcessMetrics().handle(text)


def test_wrong_operation_for_declared_type():
    m = ProcessMetrics()
    send(m, name="jobs", op="declare", type="counter")
    with pytest.raises(MetricError, match="cannot observe a counter"):
        send(m, name="jobs", op="observe", value=1)


def test_series_limit():
    m = ProcessMetrics()
    for n in range(MAX_SERIES):
        send(m, name="x", op="inc", labels={"n": n})
    with pytest.raises(MetricError, match="series"):
        send(m, name="x", op="inc", labels={"n": "one too many"})
    send(m, name="x", op="inc", labels={"n": 0})  # existing series still work


# -- the client library ------------------------------------------------------


@pytest.fixture
def emitted(monkeypatch):
    """The lines the client library writes, with the runner's flag set."""
    lines = []
    monkeypatch.setenv("CORRAL_METRICS", "1")
    monkeypatch.setattr(client, "_write", lines.append)
    monkeypatch.setattr(client, "_names", set())
    return lines


def test_client_feeds_the_runner(emitted):
    jobs = client.Counter("jobs", "Jobs done", ["queue", "kind"])
    depth = client.Gauge("depth", "Waiting")
    took = client.Histogram("took_seconds", "Time", buckets=(0.1, 1))
    size = client.Summary("size_bytes")

    jobs.labels("mail", "bulk").inc()
    jobs.labels(kind="bulk", queue="mail").inc(2)
    assert jobs.labels("mail", "bulk") is jobs.labels(queue="mail", kind="bulk")
    depth.set(5)
    depth.inc()
    depth.dec(3)
    took.observe(0.5)
    with took.time():
        pass

    @size.time()
    def work():
        return "done"

    assert work() == "done"
    with pytest.raises(KeyError), jobs.labels("mail", "single").count_exceptions():
        raise KeyError("boom")

    assert all(line.startswith("@metric {") for line in emitted)
    m = ProcessMetrics()
    for line in emitted:
        m.handle(line[len("@metric "):])
    got = samples(m)
    assert got[("jobs_total", (("kind", "bulk"), ("queue", "mail")))] == 3
    assert got[("jobs_total", (("kind", "single"), ("queue", "mail")))] == 1
    assert got[("depth", ())] == 3
    assert got[("took_seconds_bucket", (("le", "0.1"),))] == 1  # the empty "with" block
    assert got[("took_seconds_bucket", (("le", "1.0"),))] == 2
    assert got[("took_seconds_count", ())] == 2
    assert got[("size_bytes_count", ())] == 1
    assert {(n, k, h) for n, k, h, _ in m.families()} == {
        ("jobs_total", "counter", "Jobs done"),
        ("depth", "gauge", "Waiting"),
        ("took_seconds", "histogram", "Time"),
        ("size_bytes", "summary", ""),
    }


def test_client_checks_its_arguments(emitted):
    jobs = client.Counter("jobs", "Jobs done", ["queue"])
    with pytest.raises(ValueError, match="already exists"):
        client.Gauge("jobs")
    with pytest.raises(ValueError, match="labels"):
        jobs.inc()
    with pytest.raises(ValueError):
        jobs.labels("a", "b")
    with pytest.raises(ValueError):
        jobs.labels(colour="red")
    with pytest.raises(ValueError, match="increased"):
        jobs.labels("a").inc(-1)
    with pytest.raises(ValueError):
        client.Gauge("depth").set(float("nan"))
    with pytest.raises(ValueError):
        client.Gauge("9lives")
    for reserved in ("process", "app"):  # added by the runner
        with pytest.raises(ValueError, match="invalid label name"):
            client.Gauge("ok_" + reserved, labelnames=[reserved])
    with pytest.raises(ValueError):
        client.Histogram("h", buckets=(2, 1))


def test_client_is_silent_outside_the_runner(emitted, monkeypatch):
    monkeypatch.delenv("CORRAL_METRICS")
    client.Counter("jobs").inc()
    assert emitted == [] and not client.enabled()


# -- through a running process ------------------------------------------------


def test_process_metrics_reach_the_metrics_page(tmp_path, harness):  # noqa: F811
    body = (
        "import os, time\n"
        "from corral.metrics import Counter, Gauge\n"
        "assert os.environ['CORRAL_METRICS'] == '1'\n"
        "runs = Counter('batch_runs', 'Batches run', ['result'])\n"
        "runs.labels('ok').inc()\n"
        "Gauge('batch_rows').set(42)\n"
        "print('@metric this is not json')\n"
        "print('a normal line', flush=True)\n"
        "time.sleep(60)\n"
    )
    other = "print('@metric {\"name\": \"batch_rows\", \"op\": \"set\", \"value\": 7}', flush=True)\n"
    clash = "print('@metric {\"name\": \"batch_rows\", \"op\": \"inc\", \"type\": \"counter\"}', flush=True)\n"
    supervisor, store = harness(
        process(tmp_path, "batch", body),
        process(tmp_path, "other", other + "import time; time.sleep(60)\n"),
        process(tmp_path, "clash", clash + "import time; time.sleep(60)\n"),
    )
    wait_for(lambda: logged(store, "batch", "stdout") == ["a normal line"])  # metric lines are not log lines
    wait_for(lambda: supervisor.processes["other"].metrics.families())
    wait_for(lambda: supervisor.processes["clash"].metrics.families())
    assert any("ignored metric line (not valid JSON)" in line for line in logged(store, "batch", "runner"))
    assert supervisor.processes["batch"].counters()["lines"]["stdout"] == 1

    text = prometheus.render(supervisor)
    m = parse(text)
    assert m[("batch_runs_total", (("process", "batch"), ("result", "ok")))] == 1
    assert m[("batch_rows", (("process", "batch"),))] == 42
    assert m[("batch_rows", (("process", "other"),))] == 7
    assert "# HELP batch_runs_total Batches run\n# TYPE batch_runs_total counter\n" in text
    assert text.count("# TYPE batch_rows ") == 1
    assert "left out batch_rows_total" not in text  # a counter named batch_rows is batch_rows_total: no clash
    assert m[("batch_rows_total", (("process", "clash"),))] == 1

    # The values outlive the process, and a counter keeps counting on the next run.
    batch = supervisor.processes["batch"]
    batch.restart()
    wait_for(lambda: parse(prometheus.render(supervisor))[("batch_runs_total", (("process", "batch"), ("result", "ok")))] == 2)


def test_processes_disagreeing_about_a_type():
    from types import SimpleNamespace

    first, second, third = ProcessMetrics(), ProcessMetrics(), ProcessMetrics()
    send(first, name="rows", op="set", value=1)
    send(second, name="rows", op="observe", value=1)
    send(third, name="rows", op="set", value=3)
    processes = {n: SimpleNamespace(metrics=m, cfg=SimpleNamespace(app="")) for n, m in (("a", first), ("b", second), ("c", third))}
    lines = prometheus._custom(SimpleNamespace(processes=processes), set())
    assert lines == [
        "# TYPE rows gauge",
        'rows{process="a"} 1',
        'rows{process="c"} 3',
        "# corral: left out rows (histogram) from process b: name in use",
    ]


def test_counter_overflowing_to_infinity_is_still_valid_output():
    m = ProcessMetrics()
    send(m, name="big", op="inc", value=1e308)
    send(m, name="big", op="inc", value=1e308)
    assert prometheus._number(samples(m)[("big_total", ())]) == "+Inf"


def test_bad_metric_lines_do_not_stop_the_output(tmp_path, harness):  # noqa: F811
    huge = '{"name": "x", "op": "set", "value": 1' + "0" * 400 + "}"
    body = (
        "import time\n"
        "print('before', flush=True)\n"
        f"print('@metric ' + {huge!r}, flush=True)\n"
        "print('@metric ' + '[' * 60000, flush=True)\n"
        "print('after', flush=True)\n"
        "time.sleep(60)\n"
    )
    _, store = harness(process(tmp_path, "s", body))
    wait_for(lambda: logged(store, "s", "stdout") == ["before", "after"])
    complaints = [line for line in logged(store, "s", "runner") if "ignored metric line" in line]
    assert len(complaints) == 2 and "too large" in complaints[0]
    assert not [line for line in logged(store, "s", "runner") if "internal error" in line]
