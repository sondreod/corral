"""Pushing log lines to a fake Loki."""

import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from corral import loki as loki_module
from corral.config import LokiConfig
from corral.loki import LokiPusher, encode
from corral.store import LogStore
from test_supervisor import wait_for


class FakeLoki:
    """Records pushed streams; answers with ``status`` (a list is used up in
    order, then 204)."""

    def __init__(self):
        self.pushes: list[dict] = []
        self.headers: list = []
        self.status: list[int] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                code = fake.status.pop(0) if fake.status else 204
                if code == 204:
                    assert self.headers["Content-Encoding"] == "gzip"
                    fake.pushes.append(json.loads(gzip.decompress(body)))
                    fake.headers.append(self.headers)  # case-insensitive lookups
                self.send_response(code)
                message = b"entry too far behind" if code == 400 else b""
                self.send_header("Content-Length", str(len(message)))
                self.end_headers()
                self.wfile.write(message)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/loki/api/v1/push"

    def lines(self, **labels):
        """Every pushed line from streams with these labels, in push order."""
        return [
            line
            for push in self.pushes
            for stream in push["streams"]
            if labels.items() <= stream["stream"].items()
            for _, line in stream["values"]
        ]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake():
    f = FakeLoki()
    yield f
    f.close()


def pusher(url, **kwargs):
    p = LokiPusher(LokiConfig(url, **{"batch_wait": 0.05, **kwargs}))
    p.start()
    return p


def test_encode_groups_by_process_and_stream():
    batch = [
        ("shop.web", "stdout", "one", 1.5),
        ("shop.web", "stderr", "oops", 2.0),
        ("shop.web", "stdout", "two", 3.0),
        ("blog.worker", "stdout", "ünïcode", 4.0),
    ]
    body = json.loads(encode(batch, {"host": "box"}))
    assert body == {
        "streams": [
            {"stream": {"host": "box", "app": "shop", "process": "shop.web", "stream": "stdout"},
             "values": [["1500000000", "one"], ["3000000000", "two"]]},
            {"stream": {"host": "box", "app": "shop", "process": "shop.web", "stream": "stderr"},
             "values": [["2000000000", "oops"]]},
            {"stream": {"host": "box", "app": "blog", "process": "blog.worker", "stream": "stdout"},
             "values": [["4000000000", "ünïcode"]]},
        ]
    }


def test_lines_from_the_store_reach_loki(fake, tmp_path):
    p = pusher(fake.url, labels={"host": "box"}, headers={"X-Scope-OrgID": "team"})
    store = LogStore(tmp_path / "logs.sqlite3")
    store.forward = p.push
    try:
        store.append("shop.web", "stdout", "hello")
        store.append("shop.web", "runner", "exited with code 0")
        wait_for(lambda: p.sent == 2)
    finally:
        store.close()
        p.stop()
    assert fake.lines(process="shop.web", stream="stdout", app="shop", host="box") == ["hello"]
    assert fake.lines(stream="runner") == ["exited with code 0"]
    assert fake.headers[0]["X-Scope-OrgID"] == "team"
    assert fake.headers[0]["User-Agent"].startswith("corral/")


def test_retries_until_loki_is_back(fake):
    p = pusher(fake.url)
    fake.status = [503, 429]
    try:
        p.push("a.b", "stdout", "kept", 1.0)
        wait_for(lambda: p.sent == 1, timeout=10)
    finally:
        p.stop()
    assert p.failures == 2
    assert fake.lines() == ["kept"]


def test_refused_lines_are_dropped(fake, capsys):
    p = pusher(fake.url)
    fake.status = [400]
    try:
        p.push("a.b", "stdout", "too old", 1.0)
        wait_for(lambda: p.dropped["rejected"] == 1)
        p.push("a.b", "stdout", "fine", 2.0)
        wait_for(lambda: p.sent == 1)
    finally:
        p.stop()
    assert fake.lines() == ["fine"]
    assert "loki rejected 1 line(s): HTTP 400: entry too far behind" in capsys.readouterr().out


def test_full_buffer_drops_the_oldest(fake):
    fake.close()  # nothing listening: every push fails
    p = LokiPusher(LokiConfig(fake.url, max_buffer=3))  # not started
    for i in range(5):
        p.push("a.b", "stdout", f"line {i}", float(i))
    assert p.buffered() == 3
    assert p.dropped["buffer_full"] == 2
    assert [line for _, _, line, _ in p._take()] == ["line 2", "line 3", "line 4"]


def test_big_backlog_goes_in_several_requests(fake, monkeypatch):
    monkeypatch.setattr(loki_module, "MAX_BATCH", 10)
    p = LokiPusher(LokiConfig(fake.url, batch_wait=30))
    for i in range(25):
        p.push("a.b", "stdout", str(i), float(i))
    p.start()  # 25 lines are waiting: the first batch goes out without waiting 30s
    try:
        wait_for(lambda: p.sent == 20)
    finally:
        p.stop()  # sends the rest
    assert p.sent == 25
    assert [len(push["streams"][0]["values"]) for push in fake.pushes] == [10, 10, 5]
    assert fake.lines() == [str(i) for i in range(25)]


def test_stop_gives_up_when_loki_is_down(fake):
    fake.close()
    p = pusher(fake.url)
    p.push("a.b", "stdout", "lost", 1.0)
    p.stop(timeout=15)
    assert not p._thread.is_alive()
    assert p.sent == 0 and p.failures >= 1
