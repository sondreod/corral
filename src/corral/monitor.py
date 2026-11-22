"""Periodic resource measurements of each process tree.

Reads Linux's /proc directly. Where /proc is not available the resource
figures are simply absent; log line counts are still recorded.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

from .store import LogStore
from .supervisor import Supervisor

try:
    _TICKS_PER_SECOND = os.sysconf("SC_CLK_TCK")
    _PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
except (ValueError, OSError, AttributeError):
    _TICKS_PER_SECOND, _PAGE_SIZE = 100, 4096

_PRUNE_INTERVAL = 3600.0


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    session: int
    ticks: int  # user + system CPU time, in clock ticks
    rss: int  # bytes
    threads: int


def parse_stat(text: str) -> ProcInfo:
    """Parse the contents of /proc/<pid>/stat."""
    # The command name is in parentheses and may itself contain spaces and
    # parentheses, so split around the last ')'.
    head, _, tail = text.rpartition(")")
    fields = tail.split()  # fields[0] is field 3 (state) of proc(5)
    return ProcInfo(
        pid=int(head.split("(", 1)[0]),
        ppid=int(fields[1]),
        session=int(fields[3]),
        ticks=int(fields[11]) + int(fields[12]),
        rss=int(fields[21]) * _PAGE_SIZE,
        threads=int(fields[17]),
    )


def read_processes(proc: str = "/proc") -> dict[int, ProcInfo]:
    processes = {}
    try:
        entries = os.listdir(proc)
    except OSError:
        return processes
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"{proc}/{entry}/stat") as fh:
                info = parse_stat(fh.read())
        except (OSError, ValueError, IndexError):
            continue  # the process went away while we were looking
        processes[info.pid] = info
    return processes


def process_tree(processes: dict[int, ProcInfo], root: int) -> list[ProcInfo]:
    """The root process with everything that belongs to it: its descendants,
    and members of its session (processes are started as session leaders, so
    that also catches children that were re-parented after their parent died)."""
    if root not in processes:
        return []
    children: dict[int, list[int]] = {}
    for info in processes.values():
        children.setdefault(info.ppid, []).append(info.pid)
    members = {pid for pid, info in processes.items() if info.session == root}
    pending = [root, *members]
    members.add(root)
    while pending:
        for child in children.get(pending.pop(), ()):
            if child not in members:
                members.add(child)
                pending.append(child)
    return [processes[pid] for pid in members]


def _count_fds(pid: int, proc: str = "/proc") -> int:
    try:
        return len(os.listdir(f"{proc}/{pid}/fd"))
    except OSError:
        return 0


class Monitor:
    def __init__(self, supervisor: Supervisor, store: LogStore, interval: float, retention_days: float):
        self._supervisor = supervisor
        self._store = store
        self.interval = interval
        self._retention = retention_days * 86400
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest: dict[str, dict] = {}
        # Per process: (time of the previous sample, {pid: cpu ticks}).
        self._previous: dict[str, tuple[float, dict[int, int]]] = {}
        self._cpu_seconds: dict[str, float] = {}
        self._thread = threading.Thread(target=self._loop, name="monitor", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(5)

    def latest(self, name: str) -> dict | None:
        """The most recent measurement of a running process."""
        with self._lock:
            return self._latest.get(name)

    def cpu_seconds(self, name: str) -> float:
        """CPU time the process tree has used since the runner started,
        as far as the samples saw it."""
        with self._lock:
            return self._cpu_seconds.get(name, 0.0)

    def _loop(self) -> None:
        next_prune = 0.0
        while not self._stop.wait(self.interval):
            try:
                self.sample()
                if time.monotonic() >= next_prune:
                    self._store.prune_history(time.time() - self._retention)
                    next_prune = time.monotonic() + _PRUNE_INTERVAL
            except Exception as exc:  # keep measuring whatever happens
                print(f"corral: monitoring failed: {exc!r}", flush=True)

    def sample(self) -> None:
        now = time.time()
        processes = None
        for name, process in self._supervisor.processes.items():
            out_lines, err_lines = process.take_line_counts()
            root = process.root_pid()
            measured = None
            if root is not None:
                if processes is None:
                    processes = read_processes()
                measured = self._measure(name, root, processes, now)
            if measured is None:
                self._previous.pop(name, None)
            with self._lock:
                if measured is None:
                    self._latest.pop(name, None)
                else:
                    self._latest[name] = {"ts": now, **measured}
            if measured is not None or out_lines or err_lines:
                self._store.add_sample(
                    name, now, out_lines=out_lines, err_lines=err_lines, **(measured or {})
                )

    def _measure(self, name: str, root: int, processes: dict[int, ProcInfo], now: float) -> dict | None:
        tree = process_tree(processes, root)
        if not tree:
            return None
        ticks = {info.pid: info.ticks for info in tree}
        cpu = None
        previous = self._previous.get(name)
        # Per-process differences, so a child that exited does not make the
        # total go backwards. A new process contributes all its time.
        seen = previous[1] if previous is not None else {}
        used = sum(max(0, t - seen.get(pid, 0)) for pid, t in ticks.items())
        if previous is not None and now > previous[0]:
            cpu = round(100.0 * used / _TICKS_PER_SECOND / (now - previous[0]), 2)
        with self._lock:
            self._cpu_seconds[name] = self._cpu_seconds.get(name, 0.0) + used / _TICKS_PER_SECOND
        self._previous[name] = (now, ticks)
        return {
            "cpu": cpu,
            "rss": sum(info.rss for info in tree),
            "threads": sum(info.threads for info in tree),
            "fds": sum(_count_fds(info.pid) for info in tree),
        }
