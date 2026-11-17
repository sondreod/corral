import json
import math
import threading
import urllib.error
import urllib.request

import pytest

from corral import exposition
from corral.tsdb import MetricDB, QueryError, Scraper, histogram_quantile, scrape_url
from corral.web import make_server

from test_supervisor import fake_uv, harness, script, wait_for  # noqa: F401  (fixtures)

TEXT = """\
# HELP jobs_total Jobs done.
# TYPE jobs_total counter
jobs_total{script="a",kind="x"} 3
jobs_total{script="b",kind="x"} 10
# TYPE temp gauge
temp{script="a",room="hall \\"1\\"\\nnorth"} -1.5
# TYPE took_seconds histogram
took_seconds_bucket{script="a",le="0.1"} 1
took_seconds_bucket{script="a",le="1.0"} 3
took_seconds_bucket{script="a",le="+Inf"} 4
took_seconds_sum{script="a"} 2.5
took_seconds_count{script="a"} 4
# corral: left out something
loose 7 1700000000000
broken NaN
"""


def test_parse_exposition():
    samples = {(s.name, s.labels): s for s in exposition.parse(TEXT)}
    jobs = samples[("jobs_total", (("kind", "x"), ("script", "a")))]
    assert (jobs.value, jobs.family, jobs.kind, jobs.help) == (3, "jobs_total", "counter", "Jobs done.")
    temp = samples[("temp", (("room", 'hall "1"\nnorth'), ("script", "a")))]
    assert (temp.value, temp.kind) == (-1.5, "gauge")
    bucket = samples[("took_seconds_bucket", (("le", "+Inf"), ("script", "a")))]
    assert (bucket.family, bucket.kind) == ("took_seconds", "bucket")
    assert samples[("took_seconds_count", (("script", "a"),))].kind == "counter"
    assert samples[("loose", ())].kind == "untyped"
    assert not any(name == "broken" for name, _ in samples)  # NaN cannot be stored


@pytest.mark.parametrize("bad", ['x{a="b} 1', "x{a=b} 1", "x", '{a="b"} 1', "x 1 2 3", "x abc"])
def test_parse_errors(bad):
    with pytest.raises(exposition.ParseError):
        exposition.parse(bad + "\n")


def test_histogram_quantile():
    buckets = [(0.1, 10), (1.0, 30), (math.inf, 40)]
    assert histogram_quantile(0.25, buckets) == pytest.approx(0.1)
    assert histogram_quantile(0.5, buckets) == pytest.approx(0.1 + 0.9 * 10 / 20)
    assert histogram_quantile(0.99, buckets) == 1.0  # in the +Inf bucket: its lower bound
    assert histogram_quantile(0.5, [(1.0, 0), (math.inf, 0)]) is None
    assert histogram_quantile(0.5, [(1.0, 5)]) is None  # no +Inf bucket


def counter_text(a, b, h=(0, 0, 0)):
    return (
        "# TYPE jobs_total counter\n"
        f'jobs_total{{script="a"}} {a}\njobs_total{{script="b"}} {b}\n'
        "# TYPE took_seconds histogram\n"
        f'took_seconds_bucket{{le="1"}} {h[0]}\ntook_seconds_bucket{{le="2"}} {h[1]}\n'
        f'took_seconds_bucket{{le="+Inf"}} {h[2]}\n'
        "# TYPE temp gauge\n"
        f'temp{{script="a"}} {a * 10}\n'
    )


@pytest.fixture
def db(tmp_path):
    db = MetricDB(tmp_path / "m.sqlite3", interval=60, retention=86400)
    # five scrapes, one a minute; "b" restarts from zero at the fourth
    rows = [(0, 100, (0, 0, 0)), (6, 110, (1, 1, 1)), (12, 120, (2, 5, 5)), (18, 3, (2, 6, 8)), (24, 6, (4, 8, 10))]
    for i, (a, b, h) in enumerate(rows):
        db.ingest(60_000 + 60 * i, exposition.parse(counter_text(a, b, h)))
    return db


def values(result):
    return {tuple(sorted(s["labels"].items())): [v for _, v in s["values"]] for s in result["series"]}


def test_query_raw_and_counters(db):
    span = dict(start=60_000, end=60_000 + 299)
    raw = values(db.query({"metric": "temp"}, **span, points=5))
    assert raw == {(("script", "a"),): [0, 60, 120, 180, 240]}

    inc = values(db.query({"metric": "jobs_total", "fn": "increase"}, **span, points=5))
    assert inc[(("script", "a"),)] == [None, 6, 6, 6, 6]
    assert inc[(("script", "b"),)] == [None, 10, 10, 3, 3]  # the drop is a restart, not -117

    rate = values(db.query({"metric": "jobs_total", "fn": "per_minute", "agg": "sum"}, **span, points=5))
    assert rate == {(): [None, 16, 16, 9, 9]}
    per_second = values(db.query({"metric": "jobs_total", "fn": "rate", "match": {"script": "a"}}, **span, points=5))
    assert per_second == {(("script", "a"),): [None, 0.1, 0.1, 0.1, 0.1]}

    # two-minute steps add up the increases inside them
    wide = db.query({"metric": "jobs_total", "fn": "increase", "agg": "max"}, 60_000, 60_239, points=2)
    assert wide["step"] == 120 and wide["start"] == 60_000
    assert [v for _, v in wide["series"][0]["values"]] == [10, 13]  # a: 6, 12; b: 10, 13


def test_query_histograms(db):
    span = dict(start=60_000, end=60_000 + 299)
    p50 = values(db.query({"metric": "took_seconds_bucket", "fn": "quantile", "q": 0.5}, **span, points=5))
    # second step: increases 1/1/1 -> everything in the first bucket
    # third step: 1/4/4 -> rank 2 of 4 falls in (1, 2]
    assert p50[()][:3] == [None, pytest.approx(0.5), pytest.approx(1 + (2 - 1) / 3)]

    heat = db.query({"metric": "took_seconds_bucket", "fn": "heatmap"}, **span, points=5)
    by_le = {s["labels"]["le"]: [v for _, v in s["values"]] for s in heat["series"]}
    assert by_le == {"1": [None, 1, 1, 0, 2], "2": [None, 0, 3, 1, 0], "+Inf": [None, 0, 0, 2, 0]}

    with pytest.raises(QueryError, match="_bucket"):
        db.query({"metric": "jobs_total", "fn": "heatmap"}, **span)


@pytest.mark.parametrize(
    "query",
    [{}, {"metric": ""}, {"metric": "x", "fn": "sql"}, {"metric": "x", "agg": "median"},
     {"metric": "x", "match": {"a": 1}}, {"metric": "x", "by": "script"}, {"metric": "x", "fn": "quantile", "q": 2}],
)
def test_bad_queries(db, query):
    with pytest.raises(QueryError):
        db.query(query, 0, 100)


def test_catalog_and_prune(db):
    cat = {m["name"]: m for m in db.catalog()}
    assert cat["jobs_total"]["kind"] == "counter"
    assert cat["jobs_total"]["labels"] == {"script": ["a", "b"]}
    assert cat["took_seconds_bucket"]["labels"]["le"] == ["1", "2", "+Inf"]  # numeric order

    db.prune(now=60_000 + 240 + 86400)  # keeps only the last scrape
    assert values(db.query({"metric": "temp"}, 60_000, 60_299, points=5)) == {
        (("script", "a"),): [None, None, None, None, 240]
    }
    db.ingest(60_300, exposition.parse('# TYPE fresh gauge\nfresh 1\n'))
    db.prune(now=60_300 + 86400)
    assert {m["name"] for m in db.catalog()} == {"fresh"}  # emptied series are gone


def test_dashboards(db):
    panel = {"id": "p1", "type": "line", "x": 0, "y": 0, "w": 6, "h": 8,
             "query": {"metric": "temp", "fn": "raw"}}
    d = db.create_dashboard({"name": " Ops ", "spec": {"range": 3600, "panels": [panel]}})
    assert d["name"] == "Ops" and d["spec"]["panels"][0]["id"] == "p1"
    assert db.dashboards() == [{"id": d["id"], "name": "Ops", "panels": 1, "updated": d["updated"]}]
    db.update_dashboard(d["id"], {"name": "Ops 2", "spec": {"range": 600, "panels": []}})
    assert db.dashboard(d["id"])["spec"] == {"range": 600, "panels": []}
    for bad in (
        {"name": ""},
        {"name": "x", "spec": []},
        {"name": "x", "spec": {"range": 1}},
        {"name": "x", "spec": {"panels": [{**panel, "type": "pie"}]}},
        {"name": "x", "spec": {"panels": [{**panel, "x": 8}]}},  # 8 + 6 > 12 columns
        {"name": "x", "spec": {"panels": [{**panel, "query": {"metric": "temp", "fn": "nope"}}]}},
    ):
        with pytest.raises(QueryError):
            db.create_dashboard(bad)
    db.delete_dashboard(d["id"])
    assert db.dashboards() == []


def test_scrape_url():
    assert scrape_url("0.0.0.0", 80) == "http://127.0.0.1:80/metrics"
    assert scrape_url("::", 80) == "http://[::1]:80/metrics"
    assert scrape_url("192.168.1.5", 80) == "http://192.168.1.5:80/metrics"


def test_scrape_and_api_end_to_end(tmp_path, harness):  # noqa: F811
    svc = script(tmp_path, "svc", "import time\nprint('hi')\ntime.sleep(60)\n")
    supervisor, store = harness(svc)
    db = MetricDB(tmp_path / "metrics.sqlite3", interval=60)
    server = make_server("127.0.0.1", 0, supervisor, store, tsdb=db)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(path, method="GET", body=None, headers=None):
        request = urllib.request.Request(
            base + path, method=method, data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json", **({"X-Corral": "1"} if headers is None else headers)},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    try:
        wait_for(lambda: supervisor.scripts["svc"].counters()["lines"]["stdout"] == 1)
        scraper = Scraper(db, scrape_url("127.0.0.1", server.server_address[1]))
        import time
        now = int(time.time() // 60 * 60)
        assert scraper.scrape(now - 60) > 5
        assert scraper.scrape(now) > 5

        status, cat = call("/api/tsdb/metrics")
        assert status == 200 and cat["interval"] == 60
        names = {m["name"] for m in cat["metrics"]}
        assert {"corral_script_up", "corral_script_log_lines_total"} <= names

        q = json.dumps({"metric": "corral_script_up", "match": {"script": "svc"}})
        status, result = call("/api/tsdb/query?" + urllib.parse.urlencode({"q": q, "range": 600}))
        assert status == 200 and result["series"][0]["values"][-1][1] == 1
        assert call("/api/tsdb/query?q=nonsense")[0] == 400
        assert call("/api/tsdb/query?" + urllib.parse.urlencode({"q": '{"metric": "x", "fn": "no"}'}))[0] == 400

        status, d = call("/api/dashboards", "POST", {"name": "Main"})
        assert status == 201 and d["spec"] == {"range": 21600, "panels": []}
        assert call("/api/dashboards", "POST", {"name": "Other"}, headers={})[0] == 403  # no X-Corral
        assert call(f"/api/dashboards/{d['id']}", "PUT", {"name": "Renamed", "spec": {"panels": []}})[0] == 200
        assert call("/api/dashboards")[1]["dashboards"][0]["name"] == "Renamed"
        assert call(f"/api/dashboards/{d['id']}", "PUT", {"name": ""})[0] == 400
        assert call("/api/dashboards/999")[0] == 404
        assert call(f"/api/dashboards/{d['id']}", "DELETE")[0] == 200
        assert call(f"/api/dashboards/{d['id']}", "DELETE")[0] == 404
    finally:
        server.shutdown()
