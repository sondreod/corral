"""The runner's own metrics in the Prometheus text exposition format.

Everything is labelled with the script's name. After the runner's own
metrics come the ones scripts report themselves (see ``custom``). Counters start at zero when
the runner starts. Resource figures come from the monitor's most recent
sample, so they are up to one ``sample_interval`` old, and are only present
while the script is running.
"""

from __future__ import annotations

from . import __version__
from .monitor import Monitor
from .supervisor import Supervisor

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
STATES = ("stopped", "starting", "running", "stopping", "backoff", "waiting")


def _labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    escaped = (
        '{}="{}"'.format(k, v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n"))
        for k, v in labels.items()
    )
    return "{" + ",".join(escaped) + "}"


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


class _Family:
    def __init__(self, name: str, kind: str, help: str):
        self.name, self.kind, self.help = name, kind, help
        self.samples: list[tuple[dict[str, str], float]] = []

    def add(self, value: float | None, **labels: str) -> None:
        if value is not None:
            self.samples.append((labels, value))

    def lines(self) -> list[str]:
        if not self.samples:
            return []
        return [
            f"# HELP {self.name} {self.help}",
            f"# TYPE {self.name} {self.kind}",
            *(f"{self.name}{_labels(labels)} {_number(value)}" for labels, value in self.samples),
        ]


def render(supervisor: Supervisor, monitor: Monitor | None = None) -> str:
    families: list[_Family] = []

    def family(name: str, kind: str, help: str) -> _Family:
        families.append(_Family(f"scriptrunner_{name}", kind, help))
        return families[-1]

    info = family("build_info", "gauge", "The running scriptrunner version.")
    info.add(1, version=__version__)

    state = family("script_state", "gauge", "1 for the state the script is in, 0 for the others.")
    enabled = family("script_enabled", "gauge", "1 if the script is set to run, 0 if it was stopped or disabled.")
    up = family("script_up", "gauge", "1 if the script has a running process.")
    runs = family("script_runs_total", "counter", "Times the script was started.")
    failures = family(
        "script_run_failures_total",
        "counter",
        "Runs that could not start or ended with a non-zero exit code, not counting requested stops.",
    )
    lines = family("script_log_lines_total", "counter", "Lines the script printed.")
    exit_code = family(
        "script_last_exit_code", "gauge", "Exit code of the last finished run (negative: killed by that signal)."
    )
    started = family("script_last_start_time_seconds", "gauge", "When the last run started, as a Unix time.")
    exited = family("script_last_exit_time_seconds", "gauge", "When the last run ended, as a Unix time.")
    next_run = family("script_next_run_time_seconds", "gauge", "When the next run is due, as a Unix time.")
    cpu = family(
        "script_cpu_seconds_total", "counter", "CPU time used by the script's process tree, user and system."
    )
    memory = family("script_resident_memory_bytes", "gauge", "Resident memory of the script's process tree.")
    threads = family("script_threads", "gauge", "Threads in the script's process tree.")
    fds = family("script_open_fds", "gauge", "Open file descriptors in the script's process tree.")

    for name, script in supervisor.scripts.items():
        status, counters = script.status(), script.counters()
        for candidate in STATES:
            state.add(int(status["state"] == candidate), script=name, state=candidate)
        enabled.add(int(status["enabled"]), script=name)
        up.add(int(status["pid"] is not None), script=name)
        runs.add(counters["runs"], script=name)
        failures.add(counters["failures"], script=name)
        for stream, count in counters["lines"].items():
            lines.add(count, script=name, stream=stream)
        exit_code.add(status["exit_code"], script=name)
        started.add(status["started_at"], script=name)
        exited.add(status["exited_at"], script=name)
        next_run.add(status["next_run"], script=name)
        if monitor is not None:
            cpu.add(monitor.cpu_seconds(name), script=name)
            usage = monitor.latest(name) or {}
            memory.add(usage.get("rss"), script=name)
            threads.add(usage.get("threads"), script=name)
            fds.add(usage.get("fds"), script=name)

    lines_out = [line for f in families for line in f.lines()]
    lines_out += _custom(supervisor, {f.name for f in families})
    return "\n".join(lines_out) + "\n"


def _custom(supervisor: Supervisor, taken: set[str]) -> list[str]:
    """What the scripts report themselves, with a ``script`` label added.
    The same metric from several scripts becomes one family; if they disagree
    about its type, the first script wins and the others are left out."""
    merged: dict[str, tuple[str, str, list[str]]] = {}
    skipped = []
    for script_name, script in supervisor.scripts.items():
        for name, kind, help, samples in script.metrics.families():
            if name in taken or (name in merged and merged[name][0] != kind):
                skipped.append(f"# scriptrunner: left out {name} ({kind}) from script {script_name}: name in use")
                continue
            if name not in merged:
                merged[name] = (kind, help, [])
            merged[name][2].extend(
                f"{sample}{_labels({'script': script_name, **labels})} {_number(value)}"
                for sample, labels, value in samples
            )
    out = []
    for name, (kind, help, sample_lines) in merged.items():
        if help:
            out.append("# HELP {} {}".format(name, help.replace("\\", "\\\\").replace("\n", "\\n")))
        out.append(f"# TYPE {name} {kind}")
        out.extend(sample_lines)
    return out + skipped
