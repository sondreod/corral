"""Runs the processes of the configured apps and keeps track of their state.

Each process gets one thread that alternates between waiting for its turn
(restart backoff, schedule, or being stopped) and running its Procfile
command once with ``/bin/sh -c``. (The code calls them scripts, after what
corral first ran.)
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
from .store import LogStore

# A service that exits sooner than this counts as a failed start, and the
# delay before the next attempt doubles (up to ``max_restart_delay``).
QUICK_EXIT_SECONDS = 10.0
_MAX_LINE_BYTES = 64 * 1024
_MAX_METRIC_COMPLAINTS = 10  # per run


class Script:
    def __init__(self, cfg: ScriptConfig, store: LogStore):
        self.cfg = cfg
        self._store = store

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

    def reconfigure(self, cfg: ScriptConfig) -> None:
        """Take new settings (same name and mode) from a reloaded config.
        A running service whose command or environment changed is restarted;
        a scheduled one uses them from its next run."""
        with self._lock:
            old, self.cfg = self.cfg, cfg
            if cfg.enabled != old.enabled:
                self._enabled = cfg.enabled
                self._quick_exits = 0
                self._wait_since = time.time()
                if not cfg.enabled:
                    self._run_now = False
                    self._stop_current()
                elif cfg.mode == "service":
                    self._run_now = True
            elif self._proc is not None and cfg.mode == "service" and (
                (cfg.command, cfg.cwd, cfg.env, cfg.path) != (old.command, old.cwd, old.env, old.path)
            ):
                self._log("configuration changed, restarting")
                self._quick_exits = 0
                self._run_now = True
                self._stop_current()
        self._wake.set()

    def take_over(self, old: "Script") -> None:
        """Continue the totals and reported metrics of ``old``, which this
        replaces, so counters on /metrics do not start again from zero."""
        with old._lock:
            self._runs, self._failures = old._runs, old._failures
        with old._count_lock:
            self._line_totals = dict(old._line_totals)
        self.metrics = old.metrics

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
                "app": cfg.app,
                "process": cfg.process,
                "command": cfg.command,
                "cwd": str(cfg.cwd),
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
        cmd = ["/bin/sh", "-c", cfg.command]
        path = os.pathsep.join([*cfg.path, os.environ.get("PATH", os.defpath)])
        # CORRAL_METRICS tells corral.metrics in the process that someone is
        # listening for its "@metric" lines.
        env = {**os.environ, "PATH": path, "PYTHONUNBUFFERED": "1", **cfg.env, "CORRAL_METRICS": "1"}
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
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cfg.cwd,
                env=env,
                start_new_session=True,  # own process group, so we can signal the whole tree
            )
        except OSError as exc:
            self._log(f"cannot start: {exc}")
            self._store.run_finished(cfg.name, started, time.time(), None, str(exc))
            with self._lock:
                self._exited_at = time.time()
                self._exit_code = None
                self._quick_exits += 1
                self._failures += 1
            return

        self._log(f"started (pid {proc.pid}): {cfg.command}")
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
        self._drain_group(proc)
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

    def _drain_group(self, proc: subprocess.Popen) -> None:
        """The shell has exited. Whatever it left running in its process
        group (the program itself, if the shell did not exec it, or anything
        started in the background) gets SIGTERM and ``stop_timeout`` to exit,
        then SIGKILL: left alone it would keep the output pipes open."""
        if not _group_alive(proc.pid):
            return
        _signal_group(proc, signal.SIGTERM)
        deadline = time.monotonic() + self.cfg.stop_timeout
        while time.monotonic() < deadline:
            if not _group_alive(proc.pid):
                return
            time.sleep(0.05)
        self._log(f"still running after {self.cfg.stop_timeout:g}s, sending SIGKILL")
        _signal_group(proc, signal.SIGKILL)

    def _pump(self, pipe, stream: str) -> None:
        with pipe:
            for raw in iter(lambda: pipe.readline(_MAX_LINE_BYTES), b""):
                try:
                    self._ingest(stream, raw.decode("utf-8", "replace").rstrip("\r\n"))
                except Exception as exc:  # one odd line must not stop the reading
                    self._log(f"internal error handling a {stream} line: {exc!r}")

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


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


class Supervisor:
    def __init__(self, config: Config, store: LogStore):
        self._store = store
        self._reload_lock = threading.Lock()
        self.config = config
        # Replaced as a whole on reload, so readers can iterate it without a lock.
        self.scripts = {s.name: Script(s, store) for s in config.scripts}

    def reload(self, config: Config) -> dict:
        """Apply a re-read config: start new processes, stop removed ones,
        and update or restart changed ones. Returns what changed."""
        with self._reload_lock:
            old = self.scripts
            new: dict[str, Script] = {}
            added, changed, removed = [], [], []
            for cfg in config.scripts:
                script = old.get(cfg.name)
                if script is None:
                    script = Script(cfg, self._store)
                    script.begin()
                    added.append(cfg.name)
                elif script.cfg != cfg:
                    changed.append(cfg.name)
                    if script.cfg.mode == cfg.mode:
                        script.reconfigure(cfg)
                    else:  # service <-> schedule: a fresh start is simplest
                        script.shutdown()
                        replacement = Script(cfg, self._store)
                        replacement.take_over(script)
                        replacement.begin()
                        script = replacement
                new[cfg.name] = script
            for name, script in old.items():
                if name not in new:
                    script.shutdown()
                    removed.append(name)
            self.scripts = new
            self.config = config
            return {"added": added, "changed": changed, "removed": removed}

    def start(self) -> None:
        for script in self.scripts.values():
            script.begin()

    def status(self) -> list[dict]:
        return [s.status() for s in self.scripts.values()]

    def shutdown(self) -> None:
        with self._reload_lock:
            for script in self.scripts.values():
                script.shutdown()
        deadline = time.monotonic() + max(
            (s.cfg.stop_timeout for s in self.scripts.values()), default=0
        ) + 5
        for script in self.scripts.values():
            script.join(max(0.0, deadline - time.monotonic()))
