"""Runs the configured scripts and keeps track of their state.

Each script gets one thread that alternates between waiting for its turn
(restart backoff, schedule, or being stopped) and running the script once via
``uv run``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from datetime import datetime

from .config import Config, ScriptConfig
from .custom import PREFIX as METRIC_PREFIX
from .custom import MetricError, ScriptMetrics
from .project import ProjectError, project_command
from .store import LogStore

# A service that exits sooner than this counts as a failed start, and the
# delay before the next attempt doubles (up to ``max_restart_delay``).
QUICK_EXIT_SECONDS = 10.0
_MAX_LINE_BYTES = 64 * 1024
_MAX_METRIC_COMPLAINTS = 10  # per run


class Script:
    def __init__(self, cfg: ScriptConfig, store: LogStore, uv: str):
        self.cfg = cfg
        self._store = store
        self._uv = uv

        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._enabled = cfg.enabled
        self._run_now = False
        self._shutdown = False
        self._stop_gen = 0  # bumped by every request to stop the current run
        self._wait_since = time.time()
        self._proc: subprocess.Popen | None = None

        self._state = "stopped"
        self._started_at: float | None = None
        self._exited_at: float | None = None
        self._exit_code: int | None = None
        self._next_run: float | None = None
        self._runs = 0
        self._quick_exits = 0
        self._stop_requested = False  # the current run was asked to stop
        self._line_counts = {"stdout": 0, "stderr": 0}
        self._line_totals = {"stdout": 0, "stderr": 0}
        self._failures = 0
        self._count_lock = threading.Lock()
        self.metrics = ScriptMetrics()  # what the script reports with "@metric" lines
        self._metric_complaints = 0

        self._thread = threading.Thread(target=self._loop, name=f"script-{cfg.name}", daemon=True)

    # -- control (called from other threads) -----------------------------

    def begin(self) -> None:
        self._thread.start()

    def start(self) -> None:
        """Start a service, or resume a schedule."""
        with self._lock:
            if self._enabled:
                return
            self._enabled = True
            self._quick_exits = 0
            self._wait_since = time.time()
            if self.cfg.mode == "service":
                self._run_now = True
        self._wake.set()

    def stop(self) -> None:
        """Stop the current run and do not start another until asked to."""
        with self._lock:
            self._enabled = False
            self._run_now = False
            self._stop_current()
        self._wake.set()

    def restart(self) -> None:
        """Restart a service; for a scheduled script, run it now."""
        with self._lock:
            if self.cfg.mode == "service":
                self._enabled = True
            self._quick_exits = 0
            self._run_now = True
            self._stop_current()
        self._wake.set()

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown = True
            self._stop_current()
        self._wake.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread.is_alive():
            self._thread.join(timeout)

    def root_pid(self) -> int | None:
        """Pid of the running process (the leader of its session), if any."""
        with self._lock:
            return self._proc.pid if self._proc else None

    def take_line_counts(self) -> tuple[int, int]:
        """Lines printed on (stdout, stderr) since the previous call."""
        with self._count_lock:
            counts = self._line_counts["stdout"], self._line_counts["stderr"]
            self._line_counts = {"stdout": 0, "stderr": 0}
        return counts

    def counters(self) -> dict:
        """Totals since the runner started; they only ever go up."""
        with self._lock:
            runs, failures = self._runs, self._failures
        with self._count_lock:
            return {"runs": runs, "failures": failures, "lines": dict(self._line_totals)}

    def status(self) -> dict:
        cfg = self.cfg
        with self._lock:
            return {
                "name": cfg.name,
                "path": str(cfg.path or cfg.project),
                "entrypoint": cfg.entrypoint,
                "mode": cfg.mode,
                "schedule": cfg.schedule_text,
                "state": self._state,
                "enabled": self._enabled,
                "pid": self._proc.pid if self._proc else None,
                "started_at": self._started_at,
                "exited_at": self._exited_at,
                "exit_code": self._exit_code,
                "next_run": self._next_run,
                "runs": self._runs,
            }

    # -- internals -------------------------------------------------------

    def _log(self, message: str) -> None:
        self._store.append(self.cfg.name, "runner", message)

    def _stop_current(self) -> None:
        """Ask the running process (if any) to exit. Caller holds the lock."""
        self._stop_gen += 1
        proc = self._proc
        if proc is None:
            return
        self._state = "stopping"
        self._stop_requested = True
        _signal_group(proc, signal.SIGTERM)

        def escalate() -> None:
            try:
                proc.wait(self.cfg.stop_timeout)
            except subprocess.TimeoutExpired:
                self._log(f"still running after {self.cfg.stop_timeout:g}s, sending SIGKILL")
                _signal_group(proc, signal.SIGKILL)

        threading.Thread(target=escalate, daemon=True).start()

    def _due(self) -> float:
        """Wall-clock time of the next run. Caller holds the lock."""
        cfg = self.cfg
        if cfg.mode == "service":
            if self._runs == 0 or self._quick_exits == 0:
                delay = 0.0 if self._runs == 0 else cfg.restart_delay
            else:
                delay = min(cfg.restart_delay * 2 ** (self._quick_exits - 1), cfg.max_restart_delay)
            return self._wait_since + delay
        if cfg.cron is not None:
            return cfg.cron.next_after(datetime.fromtimestamp(self._wait_since)).timestamp()
        # "every": the first run is immediate, later ones are measured from
        # the start of the previous run.
        if self._started_at is None:
            return self._wait_since
        return max(self._started_at + cfg.every, self._wait_since)

    def _wait_for_turn(self) -> int | None:
        """Block until the script should run. Returns the stop generation
        seen at that moment, or None on shutdown."""
        while True:
            with self._lock:
                if self._shutdown:
                    self._state = "stopped"
                    return None
                if self._run_now:
                    self._run_now = False
                    return self._stop_gen
                if not self._enabled:
                    self._state = "stopped"
                    self._next_run = None
                    timeout = None
                else:
                    due = self._due()
                    remaining = due - time.time()
                    if remaining <= 0:
                        return self._stop_gen
                    self._state = "backoff" if self.cfg.mode == "service" else "waiting"
                    self._next_run = due
                    # Re-check regularly so wall-clock changes are noticed.
                    timeout = min(remaining, 30.0)
            self._wake.wait(timeout)
            self._wake.clear()

    def _loop(self) -> None:
        while True:
            gen = self._wait_for_turn()
            if gen is None:
                return
            try:
                self._run_once(gen)
            except Exception as exc:  # never let the supervising thread die
                self._log(f"internal error: {exc!r}")
                with self._lock:
                    self._proc = None
                    self._quick_exits += 1
                    self._failures += 1
            with self._lock:
                self._wait_since = time.time()

    def _run_once(self, gen: int) -> None:
        cfg = self.cfg
        cmd = None
        # SCRIPTRUNNER_METRICS tells scriptrunner.metrics in the script that
        # someone is listening for its "@metric" lines.
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "SCRIPTRUNNER_METRICS": "1", **cfg.env}
        started = time.time()
        with self._lock:
            self._state = "starting"
            self._next_run = None
            self._started_at = started
            self._exited_at = None
            self._runs += 1
            self._stop_requested = False
        self._metric_complaints = 0
        self._store.run_started(cfg.name, started)
        try:
            cmd = self._command()
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cfg.cwd,
                env=env,
                start_new_session=True,  # own process group, so we can signal the whole tree
            )
        except (OSError, ProjectError) as exc:
            self._log(f"failed to start {cmd[0]!r}: {exc}" if cmd else f"cannot start: {exc}")
            self._store.run_finished(cfg.name, started, time.time(), None, str(exc))
            with self._lock:
                self._exited_at = time.time()
                self._exit_code = None
                self._quick_exits += 1
                self._failures += 1
            return

        self._log(f"started (pid {proc.pid})")
        with self._lock:
            self._proc = proc
            self._state = "running"
            if self._shutdown or self._stop_gen != gen:
                self._stop_current()  # a stop arrived while we were spawning

        readers = [
            threading.Thread(target=self._pump, args=(pipe, stream), daemon=True)
            for pipe, stream in ((proc.stdout, "stdout"), (proc.stderr, "stderr"))
        ]
        for reader in readers:
            reader.start()
        code = proc.wait()
        # Anything the script left behind would keep the pipes open forever.
        _signal_group(proc, signal.SIGKILL)
        for reader in readers:
            reader.join(5)

        if code < 0:
            try:
                reason = f"killed by {signal.Signals(-code).name}"
            except ValueError:
                reason = f"killed by signal {-code}"
        else:
            reason = f"exited with code {code}"
        self._log(reason)

        ended = time.time()
        with self._lock:
            note = "stopped" if self._stop_requested else None
        self._store.run_finished(cfg.name, started, ended, code, note)
        with self._lock:
            self._proc = None
            self._exited_at = ended
            self._exit_code = code
            if code != 0 and note is None:
                self._failures += 1
            if ended - started < QUICK_EXIT_SECONDS:
                self._quick_exits += 1
            else:
                self._quick_exits = 0

    def _command(self) -> list[str]:
        cfg = self.cfg
        if cfg.project is None:
            return [self._uv, "run", "--quiet", "--script", str(cfg.path), *cfg.args]
        # The project file is read on every start, so edits to it (a renamed
        # script, say) apply on the next restart like any other source change.
        target = project_command(cfg.project, cfg.entrypoint)
        return [self._uv, "run", "--quiet", "--project", str(cfg.project), *target, *cfg.args]

    def _pump(self, pipe, stream: str) -> None:
        with pipe:
            for raw in iter(lambda: pipe.readline(_MAX_LINE_BYTES), b""):
                self._ingest(stream, raw.decode("utf-8", "replace").rstrip("\r\n"))

    def _ingest(self, stream: str, line: str) -> None:
        """Every line a script prints passes through here. Metric lines are
        applied and not kept as log lines."""
        if line.startswith(METRIC_PREFIX):
            try:
                self.metrics.handle(line[len(METRIC_PREFIX):])
            except MetricError as exc:
                self._metric_complaints += 1
                if self._metric_complaints <= _MAX_METRIC_COMPLAINTS:
                    shown = line if len(line) <= 200 else line[:200] + "…"
                    self._log(f"ignored metric line ({exc}): {shown}")
                if self._metric_complaints == _MAX_METRIC_COMPLAINTS:
                    self._log("further invalid metric lines in this run are not reported")
            return
        self._store.append(self.cfg.name, stream, line)
        with self._count_lock:
            self._line_counts[stream] += 1
            self._line_totals[stream] += 1


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


class Supervisor:
    def __init__(self, config: Config, store: LogStore):
        self.scripts = {s.name: Script(s, store, config.uv) for s in config.scripts}

    def start(self) -> None:
        for script in self.scripts.values():
            script.begin()

    def status(self) -> list[dict]:
        return [s.status() for s in self.scripts.values()]

    def shutdown(self) -> None:
        for script in self.scripts.values():
            script.shutdown()
        deadline = time.monotonic() + max(
            (s.cfg.stop_timeout for s in self.scripts.values()), default=0
        ) + 5
        for script in self.scripts.values():
            script.join(max(0.0, deadline - time.monotonic()))
