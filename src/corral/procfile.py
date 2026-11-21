"""Reading Procfiles and .env files, the way Foreman and Honcho do.

A Procfile has one process per line, ``name: command``; blank lines and
lines starting with ``#`` are skipped. A .env file has ``KEY=value`` lines,
optionally prefixed with ``export``; values may be quoted, in double quotes
with ``\\n``-style escapes or in single quotes taken literally. Variables are
not expanded (the shell running each command does that).
"""

from __future__ import annotations

import re
from pathlib import Path

PROCESS_RE = re.compile(r"^([A-Za-z0-9_-]+)\s*:\s*(.*?)\s*$")
_ENV_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "$": "$"}


class ProcfileError(ValueError):
    pass


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProcfileError(f"cannot read {path}: {exc}") from None


def parse_procfile(text: str, where: str = "Procfile") -> dict[str, str]:
    processes: dict[str, str] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = PROCESS_RE.match(line.strip())
        if not m:
            raise ProcfileError(
                f"{where} line {lineno}: expected 'name: command', where the name has only"
                " letters, digits, '_' and '-'"
            )
        name, command = m.groups()
        if not command:
            raise ProcfileError(f"{where} line {lineno}: process {name!r} has no command")
        if name in processes:
            raise ProcfileError(f"{where} line {lineno}: process {name!r} is defined twice")
        processes[name] = command
    if not processes:
        raise ProcfileError(f"{where} defines no processes")
    return processes


def read_procfile(path: Path) -> dict[str, str]:
    return parse_procfile(_read(path), str(path))


def _unquote(value: str, where: str) -> str:
    if value[:1] == '"':
        out, i = [], 1
        while i < len(value):
            ch = value[i]
            if ch == "\\" and i + 1 < len(value):
                out.append(_ESCAPES.get(value[i + 1], "\\" + value[i + 1]))
                i += 2
            elif ch == '"':
                rest = value[i + 1 :].strip()
                if rest and not rest.startswith("#"):
                    raise ProcfileError(f"{where}: unexpected text after the closing quote")
                return "".join(out)
            else:
                out.append(ch)
                i += 1
        raise ProcfileError(f"{where}: missing closing double quote")
    if value[:1] == "'":
        end = value.find("'", 1)
        if end < 0:
            raise ProcfileError(f"{where}: missing closing single quote")
        rest = value[end + 1 :].strip()
        if rest and not rest.startswith("#"):
            raise ProcfileError(f"{where}: unexpected text after the closing quote")
        return value[1:end]
    # Unquoted: a " #" starts a comment.
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def parse_env(text: str, where: str = ".env") -> dict[str, str]:
    env: dict[str, str] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _ENV_RE.match(stripped)
        if not m:
            raise ProcfileError(f"{where} line {lineno}: expected KEY=value")
        key, value = m.groups()
        env[key] = _unquote(value.strip(), f"{where} line {lineno}")
    return env


def read_env(path: Path) -> dict[str, str]:
    """The variables in ``path``, or none if it does not exist."""
    if not path.exists():
        return {}
    return parse_env(_read(path), str(path))
