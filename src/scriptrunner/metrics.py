"""Report metrics from a script run by scriptrunner.

This module is for the scripts, not for the runner: a script depends on the
``scriptrunner`` package and imports ``scriptrunner.metrics``. It uses nothing
else from the package and only the standard library.

The classes mirror ``prometheus_client``: create ``Counter``, ``Gauge``,
``Histogram`` and ``Summary`` objects once, then call ``inc``, ``set``,
``observe`` and so on. Instead of keeping the values in this process, each
call prints one line starting with ``@metric`` on standard output. The runner
recognises those lines, keeps the values and serves them on its ``/metrics``
page.

When the script is not started by scriptrunner (the SCRIPTRUNNER_METRICS
environment variable is not set) the calls do nothing.
"""

from __future__ import annotations

import functools
import json
import math
import os
import re
import sys
import threading
import time

__all__ = ["Counter", "Gauge", "Histogram", "Summary", "DEFAULT_BUCKETS", "enabled"]

DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0)

_PREFIX = "@metric "
_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_RESERVED_LABELS = ("script", "le", "quantile")

_lock = threading.Lock()
_names: set[str] = set()


def enabled() -> bool:
    """Whether metric lines are being written (the runner is listening)."""
    return os.environ.get("SCRIPTRUNNER_METRICS", "") not in ("", "0")


def _write(line: str) -> None:
    """Write one complete line to standard output, in a single write so that
    lines from different threads cannot be interleaved."""
    data = (line + "\n").encode()
    with _lock:
        try:
            sys.stdout.flush()  # keep the order relative to what was printed before
        except Exception:
            pass
        try:
            while data:
                data = data[os.write(1, data):]
        except OSError:
            pass  # standard output is gone; metrics are not worth crashing for


def _emit(message: dict) -> None:
    if enabled():
        _write(_PREFIX + json.dumps(message, separators=(",", ":")))


def _number(value, what: str = "value") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{what} must be a finite number, not {value!r}")
    return value


class _Timer:
    """Context manager and decorator that reports the time taken."""

    def __init__(self, report):
        self._report = report

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self._report(max(time.perf_counter() - self._start, 0.0))
        return False

    def __call__(self, function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            with _Timer(self._report):
                return function(*args, **kwargs)

        return wrapper


class _Metric:
    _type = ""

    def __init__(self, name: str, documentation: str = "", labelnames=(), **declaration):
        if not _NAME_RE.match(name):
            raise ValueError(f"invalid metric name {name!r}")
        if name.startswith("scriptrunner_"):
            raise ValueError("names starting with scriptrunner_ are reserved for the runner")
        labelnames = tuple(labelnames)
        for label in labelnames:
            if not _LABEL_RE.match(label) or label.startswith("__") or label in _RESERVED_LABELS:
                raise ValueError(f"invalid label name {label!r}")
        if len(set(labelnames)) != len(labelnames):
            raise ValueError("duplicate label names")
        with _lock:
            if name in _names:
                raise ValueError(f"a metric named {name!r} already exists")
            _names.add(name)
        self._name = name
        self._labelnames = labelnames
        self._labels: dict | None = None if labelnames else {}
        self._children: dict[tuple, _Metric] = {}
        _emit({"name": name, "op": "declare", "type": self._type, "help": documentation, **declaration})

    def labels(self, *values, **named):
        """The child of this metric with these label values, as in
        prometheus_client: positionally in the declared order, or by name."""
        if not self._labelnames:
            raise ValueError(f"{self._name} has no labels")
        if self._labels is not None:
            raise ValueError("labels() was already applied")
        if values and named:
            raise ValueError("pass label values either by position or by name, not both")
        if named:
            if sorted(named) != sorted(self._labelnames):
                raise ValueError(f"expected labels {', '.join(self._labelnames)}")
            values = tuple(named[label] for label in self._labelnames)
        elif len(values) != len(self._labelnames):
            raise ValueError(f"expected {len(self._labelnames)} label value(s)")
        key = tuple(str(v) for v in values)
        with _lock:
            child = self._children.get(key)
            if child is None:
                child = object.__new__(type(self))
                child._name = self._name
                child._labelnames = self._labelnames
                child._labels = dict(zip(self._labelnames, key))
                child._children = {}
                self._children[key] = child
        return child

    def _send(self, op: str, value) -> None:
        if self._labels is None:
            raise ValueError(f"{self._name} has labels; call .labels(...) first")
        message = {"name": self._name, "op": op, "value": _number(value)}
        if self._labels:
            message["labels"] = self._labels
        _emit(message)


class Counter(_Metric):
    """A number that only goes up. The runner adds ``_total`` to the name if
    it is not there, and keeps counting across runs of the script."""

    _type = "counter"

    def inc(self, amount: float = 1) -> None:
        if _number(amount, "amount") < 0:
            raise ValueError("a counter can only be increased")
        self._send("inc", amount)

    def count_exceptions(self, exception=Exception):
        """Context manager and decorator: count the exceptions raised inside."""
        return _ExceptionCounter(self, exception)


class _ExceptionCounter:
    def __init__(self, counter: Counter, exception):
        self._counter, self._exception = counter, exception

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        if kind is not None and issubclass(kind, self._exception):
            self._counter.inc()
        return False

    def __call__(self, function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            with self:
                return function(*args, **kwargs)

        return wrapper


class Gauge(_Metric):
    """A number that can go up and down. The runner keeps the last value
    after the script has exited."""

    _type = "gauge"

    def set(self, value: float) -> None:
        self._send("set", value)

    def inc(self, amount: float = 1) -> None:
        self._send("inc", amount)

    def dec(self, amount: float = 1) -> None:
        self._send("inc", -_number(amount, "amount"))

    def set_to_current_time(self) -> None:
        self.set(time.time())

    def time(self) -> _Timer:
        """Context manager and decorator: set the gauge to the seconds taken."""
        return _Timer(self.set)


class Summary(_Metric):
    """Counts observations and adds them up (``_count`` and ``_sum``)."""

    _type = "summary"

    def observe(self, amount: float) -> None:
        self._send("observe", amount)

    def time(self) -> _Timer:
        """Context manager and decorator: observe the seconds taken."""
        return _Timer(self.observe)


class Histogram(_Metric):
    """Counts observations in buckets (``_bucket``, ``_count`` and ``_sum``)."""

    _type = "histogram"

    def __init__(self, name: str, documentation: str = "", labelnames=(), buckets=DEFAULT_BUCKETS):
        buckets = [float(_number(b, "a bucket")) for b in buckets]
        if not buckets or buckets != sorted(set(buckets)):
            raise ValueError("buckets must be a non-empty increasing sequence")
        super().__init__(name, documentation, labelnames, buckets=buckets)

    def observe(self, amount: float) -> None:
        self._send("observe", amount)

    def time(self) -> _Timer:
        """Context manager and decorator: observe the seconds taken."""
        return _Timer(self.observe)
