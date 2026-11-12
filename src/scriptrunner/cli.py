from __future__ import annotations

import argparse
import signal
import sys
import threading

from . import __version__
from .config import ConfigError, load_config
from .monitor import Monitor
from .store import LogStore
from .supervisor import Supervisor
from .web import make_server

DEFAULT_CONFIG = "/etc/scriptrunner/config.toml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scriptrunner",
        description="Run PEP 723 scripts under supervision and show their status and logs on a web page.",
    )
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="config file (default: %(default)s)")
    parser.add_argument("--check", action="store_true", help="validate the config file and exit")
    parser.add_argument("--version", action="version", version=f"scriptrunner {__version__}")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"scriptrunner: {exc}", file=sys.stderr)
        return 2
    if args.check:
        print(f"{args.config}: OK, {len(config.scripts)} script(s)")
        return 0

    try:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        store = LogStore(config.data_dir / "logs.sqlite3", config.max_log_lines)
        supervisor = Supervisor(config, store)
        monitor = Monitor(supervisor, store, config.sample_interval, config.history_days)
        server = make_server(config.host, config.port, supervisor, store, monitor)
    except OSError as exc:
        print(f"scriptrunner: {exc}", file=sys.stderr)
        return 1

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    supervisor.start()
    monitor.start()
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    host = f"[{config.host}]" if ":" in config.host else config.host
    print(
        f"scriptrunner {__version__}: {len(config.scripts)} script(s), "
        f"web interface on http://{host}:{server.server_address[1]}/",
        flush=True,
    )

    stop.wait()
    print("scriptrunner: shutting down", flush=True)
    monitor.stop()
    supervisor.shutdown()
    server.shutdown()
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
