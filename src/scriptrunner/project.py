"""Running an entry point of a Python project (a directory with pyproject.toml).

The ``entrypoint`` setting is one of:

* a script name declared in the project's ``[project.scripts]`` (or
  ``[project.gui-scripts]``) table, e.g. ``"myapp-serve"``;
* an object reference in the syntax pyproject.toml itself uses for entry
  points, e.g. ``"myapp.jobs:main"``, to call a function that has no declared
  script;
* omitted, which selects the project's script if it declares exactly one.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_DOTTED = rf"{_IDENT}(?:\.{_IDENT})*"
_REFERENCE_RE = re.compile(rf"^{_DOTTED}:{_DOTTED}$")
# Script names follow the entry points specification: no colon (which is what
# tells them apart from references), no path separators, no leading dash.
_NAME_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.+-]*$")
ENTRYPOINT_RE = re.compile(rf"{_REFERENCE_RE.pattern}|{_NAME_RE.pattern}")

# Does what a generated console script does: import the module, call the
# object, exit with its return value. The reference arrives as the first
# argument and is removed, so the function sees only its own arguments.
_CALL_REFERENCE = """\
import sys
from importlib import import_module
ref = sys.argv.pop(1)
sys.argv[0] = ref
module, _, attrs = ref.partition(":")
target = import_module(module)
for attr in attrs.split("."):
    target = getattr(target, attr)
sys.exit(target())
"""


class ProjectError(Exception):
    pass


def declared_scripts(project: Path) -> tuple[dict[str, str], bool]:
    """The scripts a project declares, and whether it may have more that are
    only known to its build backend (``dynamic = ["scripts"]``)."""
    pyproject = project / "pyproject.toml"
    try:
        with pyproject.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise ProjectError(f"{pyproject} does not exist") from None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProjectError(f"cannot read {pyproject}: {exc}") from None
    table = data.get("project")
    if not isinstance(table, dict):
        raise ProjectError(f"{pyproject} has no [project] table")
    scripts: dict[str, str] = {}
    for key in ("scripts", "gui-scripts"):
        entries = table.get(key, {})
        if isinstance(entries, dict):
            scripts.update({k: v for k, v in entries.items() if isinstance(v, str)})
    dynamic = table.get("dynamic", [])
    return scripts, isinstance(dynamic, list) and bool({"scripts", "gui-scripts"} & set(dynamic))


def project_command(project: Path, entrypoint: str | None) -> list[str]:
    """What to run inside the project's environment (the arguments that follow
    ``uv run --project <dir>``)."""
    if entrypoint is not None and _REFERENCE_RE.match(entrypoint):
        if not (project / "pyproject.toml").is_file():
            raise ProjectError(f"{project / 'pyproject.toml'} does not exist")
        return ["python", "-c", _CALL_REFERENCE, entrypoint]

    scripts, dynamic = declared_scripts(project)
    choices = ", ".join(sorted(scripts)) or "none"
    if entrypoint is None:
        if len(scripts) != 1:
            raise ProjectError(
                f"no 'entrypoint' configured and {project} does not declare exactly one"
                f" script in [project.scripts] (declared: {choices})"
            )
        return list(scripts)
    if entrypoint not in scripts and not dynamic:
        raise ProjectError(
            f"{project} declares no script named {entrypoint!r} in [project.scripts]"
            f" (declared: {choices})"
        )
    return [entrypoint]
