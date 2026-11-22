"""corral as a whole: started from a config, reloaded with SIGHUP and the API."""

import json
import signal
import socket
import subprocess
import sys
import urllib.error
import urllib.request

from test_supervisor import wait_for


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_run_and_reload(tmp_path):
    shop = tmp_path / "shop"
    shop.mkdir()
    py = sys.executable
    (shop / "Procfile").write_text(f"web: {py} -c 'import time; print(\"web up\", flush=True); time.sleep(600)'\n")
    (shop / ".env").write_text("GREETING=hi\n")
    port = free_port()
    config = tmp_path / "config.toml"
    config.write_text(
        f'[server]\nport = {port}\n[runner]\ndata_dir = "data"\n[metrics]\nscrape_interval = "1h"\n'
        '[apps.shop]\ndir = "shop"\n'
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "corral", "--config", str(config)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"

    def call(path, method="GET"):
        request = urllib.request.Request(base + path, method=method, headers={"X-Corral": "1"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())
        except OSError:
            return None, None

    def names():
        status, body = call("/api/processes")
        return status == 200 and sorted(s["name"] for s in body["processes"])

    try:
        wait_for(lambda: names() == ["shop.web"], timeout=20)

        # SIGHUP: a new process in the Procfile starts
        (shop / "Procfile").write_text(
            (shop / "Procfile").read_text()
            + f"echo: {py} -c 'import os, time; print(os.environ[\"GREETING\"], flush=True); time.sleep(600)'\n"
        )
        proc.send_signal(signal.SIGHUP)
        wait_for(lambda: names() == ["shop.echo", "shop.web"])
        wait_for(lambda: [l["line"] for l in call("/api/processes/shop.echo/logs?streams=stdout")[1]["lines"]] == ["hi"])

        # a broken Procfile is refused and nothing changes
        (shop / "Procfile").write_text("this is not a procfile\n")
        status, body = call("/api/reload", "POST")
        assert status == 400 and "expected 'name: command'" in body["error"]
        assert names() == ["shop.echo", "shop.web"]

        # the API: a removed process stops
        (shop / "Procfile").write_text(f"web: {py} -c 'import time; time.sleep(600)'\n")
        status, body = call("/api/reload", "POST")
        assert status == 200
        assert body == {"added": [], "changed": ["shop.web"], "removed": ["shop.echo"], "needs_restart": []}
        assert names() == ["shop.web"]
    finally:
        proc.send_signal(signal.SIGTERM)
        output = proc.communicate(timeout=30)[0]
    assert "reloaded: added shop.echo" in output
    assert proc.returncode == 0
