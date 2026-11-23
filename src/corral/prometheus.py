"""The runner's own metrics in the Prometheus text exposition format.

Everything is labelled with the process's name (``process``, e.g.
``shop.web``) and its app (``app``). After the runner's own metrics come the ones processes report
themselves (see ``custom``). Counters start at zero when the runner starts.
Resource figures come from the monitor's most recent sample, so they are up
to one ``sample_interval`` old, and are only present while the process is
running.
"""

from __future__ import annotations

import math

from . import __version__
from .loki import LokiPusher
from .monitor import Monitor
from .supervisor import Supervisor

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
STATES = ("stopped", "starting", "running", "stopping", "backoff")


def _labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    escaped = (
        '{}="{}"'.format(k, v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n"))
        for k, v in labels.items()
    )
    return "{" + ",".join(escaped) + "}"


def _number(value: float) -> str:
    value = float(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):  # a counter can add up past the largest float
        return "+Inf" if value > 0 else "-Inf"
    return str(int(value)) if value.is_integer() else repr(value)


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


def render(supervisor: Supervisor, monitor: Monitor | None = None, loki: LokiPusher | None = None) -> str:
    families: list[_Family] = []

    def family(name: str, kind: str, help: str) -> _Family:
        families.append(_Family(f"corral_{name}", kind, help))
        return families[-1]

    info = family("build_info", "gauge", "The running corral version.")
    info.add(1, version=__version__)

    state = family("process_state", "gauge", "1 for the state the process is in, 0 for the others.")
    enabled = family("process_enabled", "gauge", "1 if the process is set to run, 0 if it was stopped or has enabled = false.")
    up = family("process_up", "gauge", "1 while the process is running.")
    runs = family("process_runs_total", "counter", "Times the process was started.")
    failures = family(
        "process_run_failures_total",
        "counter",
        "Runs that could not start or ended with a non-zero exit code, not counting requested stops.",
    )
    lines = family("process_log_lines_total", "counter", "Lines the process printed.")
    exit_code = family(
        "process_last_exit_code", "gauge", "Exit code of the last finished run (negative: killed by that signal)."
    )
    started = family("process_last_start_time_seconds", "gauge", "When the last run started, as a Unix time.")
    exited = family("process_last_exit_time_seconds", "gauge", "When the last run ended, as a Unix time.")
    next_run = family("process_next_run_time_seconds", "gauge", "When a process in backoff restarts, as a Unix time.")
    cpu = family(
        "process_cpu_seconds_total", "counter", "CPU time used by the process tree, user and system."
    )
    memory = family("process_resident_memory_bytes", "gauge", "Resident memory of the process tree.")
    threads = family("process_threads", "gauge", "Threads in the process tree.")
    fds = family("process_open_fds", "gauge", "Open file descriptors in the process tree.")

    for name, process in supervisor.processes.items():
        status, counters = process.status(), process.counters()
        ids = _ids(name, process)
        for candidate in STATES:
            state.add(int(status["state"] == candidate), **ids, state=candidate)
        enabled.add(int(status["enabled"]), **ids)
        up.add(int(status["pid"] is not None), **ids)
        runs.add(counters["runs"], **ids)
        failures.add(counters["failures"], **ids)
        for stream, count in counters["lines"].items():
            lines.add(count, **ids, stream=stream)
        exit_code.add(status["exit_code"], **ids)
        started.add(status["started_at"], **ids)
        exited.add(status["exited_at"], **ids)
        next_run.add(status["next_run"], **ids)
        if monitor is not None:
            cpu.add(monitor.cpu_seconds(name), **ids)
            usage = monitor.latest(name) or {}
            memory.add(usage.get("rss"), **ids)
            threads.add(usage.get("threads"), **ids)
            fds.add(usage.get("fds"), **ids)

    if loki is not None:
        family("loki_lines_sent_total", "counter", "Log lines accepted by Loki.").add(loki.sent)
        dropped = family(
            "loki_lines_dropped_total",
            "counter",
            "Log lines not sent to Loki: the buffer was full, or Loki refused them.",
        )
        for reason in ("buffer_full", "rejected"):
            dropped.add(loki.dropped[reason], reason=reason)
        family("loki_push_failures_total", "counter", "Pushes to Loki that failed and were retried.").add(
            loki.failures
        )
        family("loki_buffered_lines", "gauge", "Log lines waiting to be sent to Loki.").add(loki.buffered())

    lines_out = [line for f in families for line in f.lines()]
    lines_out += _custom(supervisor, {f.name for f in families})
    return "\n".join(lines_out) + "\n"


def _ids(name: str, process) -> dict[str, str]:
    """The labels that say which process a sample is about."""
    return {"process": name, "app": process.cfg.app} if process.cfg.app else {"process": name}


def _custom(supervisor: Supervisor, taken: set[str]) -> list[str]:
    """What the processes report themselves, with a ``process`` label added.
    The same metric from several processes becomes one family; if they disagree
    about its type, the first process wins and the others are left out."""
    merged: dict[str, tuple[str, str, list[str]]] = {}
    skipped = []
    for process_name, process in supervisor.processes.items():
        for name, kind, help, samples in process.metrics.families():
            if name in taken or (name in merged and merged[name][0] != kind):
                skipped.append(f"# corral: left out {name} ({kind}) from process {process_name}: name in use")
                continue
            if name not in merged:
                merged[name] = (kind, help, [])
            merged[name][2].extend(
                f"{sample}{_labels({**_ids(process_name, process), **labels})} {_number(value)}"
                for sample, labels, value in samples
            )
    out = []
    for name, (kind, help, sample_lines) in merged.items():
        if help:
            out.append("# HELP {} {}".format(name, help.replace("\\", "\\\\").replace("\n", "\\n")))
        out.append(f"# TYPE {name} {kind}")
        out.extend(sample_lines)
    return out + skipped
