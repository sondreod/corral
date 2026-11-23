"""Configuration file loading.

The configuration is a TOML file; see ``packaging/config.toml`` for a
commented example.
"""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .procfile import ProcfileError, read_env, read_procfile

# No dots: a process is named "<app>.<process>".
APP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$")


def parse_duration(value) -> float:
    """Seconds from a number or a string such as ``"30s"``, ``"5m"``, ``"1h"``."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
    elif isinstance(value, str) and (m := _DURATION_RE.match(value)):
        seconds = float(m.group(1)) * _UNITS[m.group(2) or "s"]
    else:
        raise ValueError(f"invalid duration {value!r} (use e.g. 30, \"30s\", \"5m\", \"1h\")")
    if not math.isfinite(seconds):
        raise ValueError("duration must be a finite number")
    if seconds <= 0:
        raise ValueError("duration must be positive")
    return seconds


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ProcessConfig:
    """One process of an app: a line of its Procfile plus the settings for it."""

    name: str  # "<app>.<process>"
    command: str  # run with /bin/sh -c in ``cwd``
    cwd: Path = Path(".")
    app: str = ""
    process: str = ""
    # The app's .env, then its env table, then the process's (later wins).
    env: dict[str, str] = field(default_factory=dict)
    # Directories put in front of PATH ([runner] path).
    path: tuple[str, ...] = ()
    enabled: bool = True
    restart_delay: float = 2.0
    max_restart_delay: float = 60.0
    stop_timeout: float = 10.0


@dataclass(frozen=True)
class AppConfig:
    name: str
    dir: Path
    procfile: Path


LABEL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LOKI_LABELS = ("app", "process", "stream")  # set by corral on every line


@dataclass(frozen=True)
class LokiConfig:
    """Where to push log lines: Loki, or anything taking its push API
    (VictoriaLogs, Grafana Cloud)."""

    url: str  # the full push URL, e.g. http://loki:3100/loki/api/v1/push
    labels: dict[str, str] = field(default_factory=dict)  # added to every line
    headers: dict[str, str] = field(default_factory=dict)  # e.g. Authorization
    batch_wait: float = 1.0  # seconds lines may wait to be sent with others
    max_buffer: int = 10_000  # lines held while the server is unreachable


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = 8723
    # Host names the web interface answers to besides localhost; "*" for any.
    allowed_hosts: tuple[str, ...] = ()
    data_dir: Path = Path("/var/lib/corral")
    path: tuple[str, ...] = ()
    max_log_lines: int = 100_000
    sample_interval: float = 10.0
    history_days: float = 7.0
    # stored metrics: how often /metrics is scraped, and how long samples are kept
    scrape_interval: float = 60.0
    metrics_retention: float = 30 * 86400.0
    loki: LokiConfig | None = None
    apps: tuple[AppConfig, ...] = ()
    processes: tuple[ProcessConfig, ...] = ()  # every process of every app


def _take(table: dict, key: str, kind, default):
    """Pop ``key`` from ``table``, checking its type."""
    if key not in table:
        return default
    value = table.pop(key)
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)
    if not isinstance(value, kind) or (kind is not bool and isinstance(value, bool)):
        raise ConfigError(f"{key!r} must be of type {kind.__name__}")
    if kind is float and not math.isfinite(value):
        raise ConfigError(f"{key!r} must be a finite number")
    return value


def _positive(key: str, value: float) -> float:
    if value <= 0:
        raise ConfigError(f"{key!r} must be positive")
    return value


def _table(raw: dict, key: str, form: str | None = None) -> dict:
    value = raw.pop(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{key!r} must be a table, written {form or f'[{key}]'}")
    return dict(value)


def _no_leftovers(table: dict, where: str) -> None:
    if table:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(sorted(table))}")


def _env(table: dict) -> dict[str, str]:
    env = _take(table, "env", dict, {})
    if not all(isinstance(v, str) for v in env.values()):
        raise ConfigError("'env' values must be strings")
    return env


def _process_settings(table: dict, defaults: dict) -> dict:
    """Settings an app gives all its processes and a process may override."""
    out = dict(defaults)
    if "enabled" in table:
        out["enabled"] = _take(table, "enabled", bool, True)
    for key in ("restart_delay", "max_restart_delay", "stop_timeout"):
        if key in table:
            out[key] = _positive(key, _take(table, key, float, 0.0))
    if out["max_restart_delay"] < out["restart_delay"]:
        raise ConfigError("'max_restart_delay' must not be less than 'restart_delay'")
    return out


def _no_schedule(table: dict) -> None:
    if "every" in table or "cron" in table:
        raise ConfigError(
            "'every' and 'cron' are no longer supported: every process runs as a service;"
            " run scheduled jobs from cron or a systemd timer instead"
        )


def _strings(table: dict, key: str) -> dict[str, str]:
    value = _take(table, key, dict, {})
    if not all(isinstance(v, str) for v in value.values()):
        raise ConfigError(f"{key!r} values must be strings")
    return value


def _loki(table: dict) -> LokiConfig:
    url = _take(table, "url", str, "")
    if not url.startswith(("http://", "https://")):
        raise ConfigError(
            "'url' is required: the push URL, such as http://localhost:3100/loki/api/v1/push"
            " (Loki) or http://localhost:9428/insert/loki/api/v1/push (VictoriaLogs)"
        )
    labels = _strings(table, "labels")
    for name in labels:
        if not LABEL_RE.match(name):
            raise ConfigError(f"label name {name!r} may only contain letters, digits and '_'")
        if name in LOKI_LABELS:
            raise ConfigError(f"label {name!r} is set by corral")
    headers = _strings(table, "headers")
    try:
        batch_wait = parse_duration(table.pop("batch_wait", "1s"))
    except ValueError as exc:
        raise ConfigError(f"'batch_wait': {exc}") from None
    max_buffer = _take(table, "max_buffer", int, 10_000)
    if max_buffer < 1:
        raise ConfigError("'max_buffer' must be positive")
    _no_leftovers(table, "[loki]")
    return LokiConfig(url, labels, headers, batch_wait, max_buffer)


def _app(name: str, table: dict, base: Path, path: tuple[str, ...]) -> tuple[AppConfig, list[ProcessConfig]]:
    if not APP_RE.match(name):
        raise ConfigError("name may only contain letters, digits, '_' and '-'")
    table = dict(table)
    dir_raw = _take(table, "dir", str, None)
    if not dir_raw:
        raise ConfigError("'dir' (the folder with the Procfile) is required")
    app_dir = base / dir_raw
    procfile = app_dir / _take(table, "procfile", str, "Procfile")
    app_env = _env(table)
    defaults = _process_settings(
        table,
        {"enabled": True, "restart_delay": 2.0, "max_restart_delay": 60.0, "stop_timeout": 10.0},
    )
    overrides = table.pop("processes", {})
    if not isinstance(overrides, dict):
        raise ConfigError("'processes' must be a table, written [apps.<app>.processes.<process>]")
    _no_schedule(table)
    _no_leftovers(table, f"[apps.{name}]")

    try:
        commands = read_procfile(procfile)
        dotenv = read_env(app_dir / ".env")
    except ProcfileError as exc:
        raise ConfigError(str(exc)) from None
    unknown = sorted(set(overrides) - set(commands))
    if unknown:
        raise ConfigError(
            f"{procfile} has no process named {', '.join(map(repr, unknown))}"
            f" (it has {', '.join(commands)})"
        )

    processes = []
    for process, command in commands.items():
        own = overrides.get(process, {})
        if not isinstance(own, dict):
            raise ConfigError(f"[apps.{name}.processes.{process}] must be a table")
        own = dict(own)
        try:
            env = {**dotenv, **app_env, **_env(own)}
            settings = _process_settings(own, defaults)
            _no_schedule(own)
            _no_leftovers(own, f"[apps.{name}.processes.{process}]")
        except ConfigError as exc:
            raise ConfigError(f"[processes.{process}]: {exc}") from None
        processes.append(
            ProcessConfig(
                name=f"{name}.{process}", command=command, cwd=app_dir, app=name, process=process,
                env=env, path=path, **settings,
            )
        )
    return AppConfig(name, app_dir, procfile), processes


def load_config(path: str | Path) -> Config:
    path = Path(path)
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None

    if "scripts" in raw:
        raise ConfigError(
            f"{path}: [scripts.*] is no longer supported; put the commands in a Procfile"
            " and point [apps.<name>] dir at its folder"
        )
    # Relative paths in the file are relative to the file itself.
    base = path.resolve().parent
    try:
        server = _table(raw, "server")
        runner = _table(raw, "runner")
        metrics = _table(raw, "metrics")
        try:
            loki = _loki(_table(raw, "loki")) if "loki" in raw else None
        except ConfigError as exc:
            raise ConfigError(f"[loki]: {exc}") from None
        apps_raw = _table(raw, "apps", "[apps.<name>]")
        _no_leftovers(raw, "top level")

        host = _take(server, "host", str, "127.0.0.1")
        if not host:
            raise ConfigError("'host' must not be empty")
        port = _take(server, "port", int, 8723)
        if not 1 <= port <= 65535:
            raise ConfigError("'port' must be between 1 and 65535")
        allowed_hosts = _take(server, "allowed_hosts", list, [])
        if not all(isinstance(h, str) and h.strip() for h in allowed_hosts):
            raise ConfigError("'allowed_hosts' must be a list of host names")
        _no_leftovers(server, "[server]")

        data_dir_raw = _take(runner, "data_dir", str, "/var/lib/corral")
        if not data_dir_raw:
            raise ConfigError("'data_dir' must not be empty")
        data_dir = base / data_dir_raw
        if "uv" in runner:
            raise ConfigError(
                "'uv' is no longer used: Procfile commands run as written; add the folder"
                " with uv to 'path' if it is not on PATH"
            )
        path_raw = _take(runner, "path", list, [])
        if not all(isinstance(d, str) and d for d in path_raw):
            raise ConfigError("'path' must be a list of directories")
        path_dirs = tuple(str(base / d) for d in path_raw)
        max_log_lines = _take(runner, "max_log_lines", int, 100_000)
        try:
            sample_interval = parse_duration(runner.pop("sample_interval", 10))
        except ValueError as exc:
            raise ConfigError(f"'sample_interval': {exc}") from None
        history_days = _take(runner, "history_days", float, 7.0)
        _no_leftovers(runner, "[runner]")
        if max_log_lines < 1:
            raise ConfigError("'max_log_lines' must be positive")
        if history_days <= 0:
            raise ConfigError("'history_days' must be positive")

        try:
            scrape_interval = parse_duration(metrics.pop("scrape_interval", "1m"))
        except ValueError as exc:
            raise ConfigError(f"'scrape_interval': {exc}") from None
        if scrape_interval < 1:
            raise ConfigError("'scrape_interval' must be at least one second")
        try:
            metrics_retention = parse_duration(metrics.pop("retention", "30d"))
        except ValueError as exc:
            raise ConfigError(f"'retention': {exc}") from None
        if metrics_retention < scrape_interval:
            raise ConfigError("'retention' must not be shorter than 'scrape_interval'")
        _no_leftovers(metrics, "[metrics]")
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from None

    apps, processes = [], []
    for name, table in apps_raw.items():
        try:
            if not isinstance(table, dict):
                raise ConfigError("must be a table")
            app, app_processes = _app(name, table, base, path_dirs)
        except ConfigError as exc:
            raise ConfigError(f"{path}: [apps.{name}]: {exc}") from None
        apps.append(app)
        processes.extend(app_processes)

    return Config(
        host=host,
        port=port,
        allowed_hosts=tuple(h.strip().lower() for h in allowed_hosts),
        data_dir=data_dir,
        path=path_dirs,
        max_log_lines=max_log_lines,
        sample_interval=sample_interval,
        history_days=history_days,
        scrape_interval=scrape_interval,
        metrics_retention=metrics_retention,
        loki=loki,
        apps=tuple(apps),
        processes=tuple(processes),
    )
