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

from .project import ENTRYPOINT_RE
from .schedule import Cron, parse_duration

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ScriptConfig:
    name: str
    # Exactly one of these is set: a PEP 723 script, or a project directory
    # (containing pyproject.toml) of which ``entrypoint`` is run.
    path: Path | None = None
    project: Path | None = None
    entrypoint: str | None = None
    mode: str = "service"  # "service" or "schedule"
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    enabled: bool = True
    # service mode
    restart_delay: float = 2.0
    max_restart_delay: float = 60.0
    # schedule mode: exactly one of these is set
    every: float | None = None
    cron: Cron | None = None
    stop_timeout: float = 10.0

    @property
    def schedule_text(self) -> str | None:
        if self.cron is not None:
            return f"cron {self.cron.expression}"
        if self.every is not None:
            return f"every {self.every:g}s"
        return None


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = 8723
    # Host names the web interface answers to besides localhost; "*" for any.
    allowed_hosts: tuple[str, ...] = ()
    data_dir: Path = Path("/var/lib/scriptrunner")
    uv: str = "uv"
    max_log_lines: int = 100_000
    sample_interval: float = 10.0
    history_days: float = 7.0
    scripts: tuple[ScriptConfig, ...] = ()


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


def _table(raw: dict, key: str) -> dict:
    value = raw.pop(key, {})
    if not isinstance(value, dict):
        form = f"[{key}.<name>]" if key == "scripts" else f"[{key}]"
        raise ConfigError(f"{key!r} must be a table, written {form}")
    return dict(value)


def _no_leftovers(table: dict, where: str) -> None:
    if table:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(sorted(table))}")


def _script(name: str, table: dict, base: Path) -> ScriptConfig:
    if not NAME_RE.match(name):
        raise ConfigError("name may only contain letters, digits, '_', '.' and '-'")
    table = dict(table)
    path = _take(table, "path", str, None)
    project = _take(table, "project", str, None)
    entrypoint = _take(table, "entrypoint", str, None)
    if bool(path) == bool(project):
        raise ConfigError("exactly one of 'path' (a script) or 'project' (a directory) is required")
    if entrypoint is not None:
        if not project:
            raise ConfigError("'entrypoint' is only valid together with 'project'")
        if not ENTRYPOINT_RE.match(entrypoint):
            raise ConfigError(
                f"invalid entrypoint {entrypoint!r}: use a script name from [project.scripts]"
                " or a 'module:function' reference"
            )
    mode = _take(table, "mode", str, "service")
    if mode not in ("service", "schedule"):
        raise ConfigError("'mode' must be \"service\" or \"schedule\"")

    args = _take(table, "args", list, [])
    env = _take(table, "env", dict, {})
    if not all(isinstance(a, str) for a in args):
        raise ConfigError("'args' must be a list of strings")
    if not all(isinstance(v, str) for v in env.values()):
        raise ConfigError("'env' values must be strings")
    cwd = _take(table, "cwd", str, None)
    if cwd == "":
        raise ConfigError("'cwd' must not be empty")

    every = cron = None
    every_raw = table.pop("every", None)
    cron_raw = _take(table, "cron", str, None)
    if mode == "schedule":
        if (every_raw is None) == (cron_raw is None):
            raise ConfigError("schedule mode needs exactly one of 'every' or 'cron'")
        try:
            if every_raw is not None:
                every = parse_duration(every_raw)
            else:
                cron = Cron(cron_raw)
        except ValueError as exc:
            raise ConfigError(str(exc)) from None
    elif every_raw is not None or cron_raw is not None:
        raise ConfigError("'every' and 'cron' are only valid with mode = \"schedule\"")

    restart_delay = _positive("restart_delay", _take(table, "restart_delay", float, 2.0))
    max_restart_delay = _positive("max_restart_delay", _take(table, "max_restart_delay", float, 60.0))
    if max_restart_delay < restart_delay:
        raise ConfigError("'max_restart_delay' must not be less than 'restart_delay'")
    stop_timeout = _positive("stop_timeout", _take(table, "stop_timeout", float, 10.0))

    cfg = ScriptConfig(
        name=name,
        path=base / path if path else None,
        project=base / project if project else None,
        entrypoint=entrypoint,
        mode=mode,
        args=tuple(args),
        env=env,
        cwd=base / cwd if cwd else None,
        enabled=_take(table, "enabled", bool, True),
        restart_delay=restart_delay,
        max_restart_delay=max_restart_delay,
        every=every,
        cron=cron,
        stop_timeout=stop_timeout,
    )
    _no_leftovers(table, "script")
    return cfg


def load_config(path: str | Path) -> Config:
    path = Path(path)
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None

    # Relative paths in the file are relative to the file itself.
    base = path.resolve().parent
    try:
        server = _table(raw, "server")
        runner = _table(raw, "runner")
        scripts_raw = _table(raw, "scripts")
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

        data_dir_raw = _take(runner, "data_dir", str, "/var/lib/scriptrunner")
        if not data_dir_raw:
            raise ConfigError("'data_dir' must not be empty")
        data_dir = base / data_dir_raw
        uv = _take(runner, "uv", str, "uv")
        if not uv:
            raise ConfigError("'uv' must not be empty")
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
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from None

    scripts = []
    for name, table in scripts_raw.items():
        try:
            if not isinstance(table, dict):
                raise ConfigError("must be a table")
            scripts.append(_script(name, table, base))
        except ConfigError as exc:
            raise ConfigError(f"{path}: [scripts.{name}]: {exc}") from None

    return Config(
        host=host,
        port=port,
        allowed_hosts=tuple(h.strip().lower() for h in allowed_hosts),
        data_dir=data_dir,
        uv=uv,
        max_log_lines=max_log_lines,
        sample_interval=sample_interval,
        history_days=history_days,
        scripts=tuple(scripts),
    )
