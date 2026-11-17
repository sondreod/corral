from __future__ import annotations

import argparse
import signal
import sqlite3
import sys
import threading

from . import __version__
from .config import ConfigError, load_config
from .monitor import Monitor
from .store import LogStore
from .supervisor import Supervisor
from .tsdb import MetricDB, Scraper, scrape_url
from .web import make_server

DEFAULT_CONFIG = "/etc/corral/config.toml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="corral",
        description="Run PEP 723 scripts under supervision and show their status and logs on a web page.",
    )
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="config file (default: %(default)s)")
    parser.add_argument("--check", action="store_true", help="validate the config file and exit")
    parser.add_argument("--version", action="version", version=f"corral {__version__}")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"corral: {exc}", file=sys.stderr)
        return 2
    if args.check:
        print(f"{args.config}: OK, {len(config.scripts)} script(s)")
        return 0

    try:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        store = LogStore(config.data_dir / "logs.sqlite3", config.max_log_lines)
    except (OSError, sqlite3.Error) as exc:
        print(f"corral: cannot open the log database in {config.data_dir}: {exc}", file=sys.stderr)
        return 1
    try:
        metric_db = MetricDB(config.data_dir / "metrics.sqlite3", config.scrape_interval, config.metrics_retention)
    except (OSError, sqlite3.Error) as exc:
        print(f"corral: cannot open the metrics database in {config.data_dir}: {exc}", file=sys.stderr)
        store.close()
        return 1
    supervisor = Supervisor(config, store)
    monitor = Monitor(supervisor, store, config.sample_interval, config.history_days)
    try:
        server = make_server(
            config.host, config.port, supervisor, store, monitor, config.allowed_hosts, metric_db
        )
    except OSError as exc:
        print(f"corral: cannot listen on {config.host} port {config.port}: {exc}", file=sys.stderr)
        store.close()
        return 1

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    supervisor.start()
    monitor.start()
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    scraper = Scraper(metric_db, scrape_url(config.host, server.server_address[1]))
    scraper.start()
    host = f"[{config.host}]" if ":" in config.host else config.host
    print(
        f"corral {__version__}: {len(config.scripts)} script(s), "
        f"web interface on http://{host}:{server.server_address[1]}/",
        flush=True,
    )

    stop.wait()
    print("corral: shutting down", flush=True)
    scraper.stop()
    monitor.stop()
    supervisor.shutdown()
    server.shutdown()
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
