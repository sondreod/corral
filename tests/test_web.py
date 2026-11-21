import threading
import urllib.error
import urllib.request

import pytest

from corral.web import host_filter, make_server

from test_supervisor import harness, script  # noqa: F401  (fixtures)


@pytest.mark.parametrize(
    "bind, allowed, header, ok",
    [
        ("127.0.0.1", (), "localhost:8723", True),
        ("127.0.0.1", (), "127.0.0.1:8723", True),
        ("127.0.0.1", (), "127.0.0.2", True),
        ("127.0.0.1", (), "[::1]:8723", True),
        ("127.0.0.1", (), "app.localhost:8723", True),
        ("127.0.0.1", (), "LOCALHOST", True),
        ("127.0.0.1", (), "evil.example:8723", False),
        ("127.0.0.1", (), "10.0.0.5:8723", False),
        ("127.0.0.1", (), "localhost.evil.example", False),
        ("::1", (), "evil.example", False),
        ("localhost", (), "evil.example", False),
        ("127.0.0.1", ("dash.example",), "Dash.Example:443", True),
        ("127.0.0.1", ("dash.example",), "other.example", False),
        ("127.0.0.1", ("*",), "anything.example", True),
        ("0.0.0.0", (), "anything.example", True),
        ("0.0.0.0", ("dash.example",), "anything.example", False),
        ("0.0.0.0", ("dash.example",), "dash.example:8723", True),
    ],
)
def test_host_filter(bind, allowed, header, ok):
    check = host_filter(bind, allowed)
    assert (check is None or check(header)) is ok


def test_foreign_host_names_are_refused(tmp_path, harness):  # noqa: F811
    supervisor, store = harness(script(tmp_path, "svc", "import time\ntime.sleep(60)\n"))
    server = make_server("127.0.0.1", 0, supervisor, store)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def status(path, host):
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Host": host})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    try:
        assert status("/api/scripts", f"localhost:{port}") == 200
        assert status("/metrics", f"127.0.0.1:{port}") == 200
        assert status("/api/scripts", f"rebound.example:{port}") == 403
        assert status("/", "rebound.example") == 403
    finally:
        server.shutdown()
