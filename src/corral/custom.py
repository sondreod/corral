"""Metrics that processes report about themselves.

A process reports a metric by printing a line that starts with ``@metric``
followed by a JSON object:

    @metric {"name": "jobs", "op": "declare", "type": "counter", "help": "Jobs done"}
    @metric {"name": "jobs", "op": "inc", "value": 1, "labels": {"queue": "mail"}}

``op`` is one of

    declare   describe the metric: ``type`` (counter, gauge, histogram or
              summary), and optionally ``help`` and, for a histogram,
              ``buckets`` (upper bounds)
    inc       add ``value`` (default 1) to a counter or gauge
    set       set a gauge to ``value``
    observe   record ``value`` in a histogram or summary

A metric that was never declared gets its type from the first operation: a
counter for ``inc``, a gauge for ``set``, a histogram for ``observe``; a
``type`` given on that line overrides the guess.

The values live in the runner, not in the process, so they survive the process
exiting: a counter keeps counting across restarts.
"""

from __future__ import annotations

import json
import math
import re
import threading

PREFIX = "@metric "
TYPES = ("counter", "gauge", "histogram", "summary")
DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0)
MAX_SERIES = 2000  # per process, so a runaway label cannot eat the runner's memory

_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_RESERVED_LABELS = ("process", "app", "le", "quantile")
_OPS = {"inc": ("counter", "gauge"), "set": ("gauge",), "observe": ("histogram", "summary")}
_GUESS = {"inc": "counter", "set": "gauge", "observe": "histogram"}


class MetricError(ValueError):
    pass


def _finite(value, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MetricError(f"{what} must be a finite number")
    try:
        value = float(value)  # an int of a few hundred digits does not fit
    except OverflowError:
        raise MetricError(f"{what} is too large") from None
    if not math.isfinite(value):
        raise MetricError(f"{what} must be a finite number")
    return value


class _Metric:
    def __init__(self, kind: str, help: str, buckets: tuple[float, ...]):
        self.kind = kind
        self.help = help
        self.buckets = buckets
        # label values (sorted pairs) -> a number, or for histograms and
        # summaries [count, sum, per-bucket counts].
        self.series: dict[tuple[tuple[str, str], ...], float | list] = {}


class ProcessMetrics:
    """The metrics one process has reported."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._metrics: dict[str, _Metric] = {}
        self._series = 0

    def handle(self, text: str) -> None:
        """Apply the part of a metric line after the prefix; raises MetricError."""
        try:
            message = json.loads(text)
        except ValueError:
            raise MetricError("not valid JSON") from None
        except RecursionError:
            raise MetricError("JSON nested too deeply") from None
        if not isinstance(message, dict):
            raise MetricError("expected a JSON object")
        unknown = set(message) - {"name", "op", "type", "help", "buckets", "value", "labels"}
        if unknown:
            raise MetricError(f"unknown key(s): {', '.join(sorted(map(str, unknown)))}")

        name, op = message.get("name"), message.get("op")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise MetricError("name must be a valid Prometheus metric name")
        if name.startswith("corral_"):
            raise MetricError("names starting with corral_ are reserved for the runner")
        if op != "declare" and op not in _OPS:
            raise MetricError("op must be declare, inc, set or observe")
        kind = message.get("type")
        if kind is not None and kind not in TYPES:
            raise MetricError(f"type must be one of {', '.join(TYPES)}")
        help = message.get("help", "")
        if not isinstance(help, str):
            raise MetricError("help must be a string")
        buckets = self._buckets(message.get("buckets"))

        with self._lock:
            metric = self._metrics.get(name)
            if op == "declare":
                if kind is None:
                    raise MetricError("declare needs a type")
                if buckets and kind != "histogram":
                    raise MetricError("only a histogram has buckets")
                buckets = (buckets or DEFAULT_BUCKETS) if kind == "histogram" else ()
                if metric is None or (metric.kind, metric.buckets) != (kind, buckets):
                    # New, or the process changed its mind: start over.
                    if metric is not None:
                        self._series -= len(metric.series)
                    self._metrics[name] = _Metric(kind, help, buckets)
                else:
                    metric.help = help
                return

            if metric is None:
                kind = kind or _GUESS[op]
                metric = _Metric(kind, help, (buckets or DEFAULT_BUCKETS) if kind == "histogram" else ())
                if kind not in _OPS[op]:
                    raise MetricError(f"cannot {op} a {kind}")
                self._metrics[name] = metric
            elif metric.kind not in _OPS[op]:
                raise MetricError(f"cannot {op} a {metric.kind}")

            value = _finite(message.get("value", 1 if op == "inc" else None), "value")
            if metric.kind == "counter" and value < 0:
                raise MetricError("a counter can only go up")
            key = self._labels(message.get("labels"))
            if key not in metric.series:
                if self._series >= MAX_SERIES:
                    raise MetricError(f"more than {MAX_SERIES} series; check the label values")
                self._series += 1
                metric.series[key] = 0.0 if op != "observe" else [0, 0.0, [0] * len(metric.buckets)]
            if op == "set":
                metric.series[key] = value
            elif op == "inc":
                metric.series[key] += value
            else:
                state = metric.series[key]
                state[0] += 1
                state[1] += value
                for i, bound in enumerate(metric.buckets):
                    if value <= bound:
                        state[2][i] += 1
                        break

    @staticmethod
    def _buckets(raw) -> tuple[float, ...]:
        if raw is None:
            return ()
        if not isinstance(raw, list) or not raw:
            raise MetricError("buckets must be a non-empty list of numbers")
        buckets = tuple(_finite(b, "a bucket") for b in raw)
        if list(buckets) != sorted(set(buckets)):
            raise MetricError("buckets must be in increasing order")
        return buckets

    @staticmethod
    def _labels(raw) -> tuple[tuple[str, str], ...]:
        if raw is None:
            return ()
        if not isinstance(raw, dict):
            raise MetricError("labels must be an object")
        for key, value in raw.items():
            if not _LABEL_RE.match(key) or key.startswith("__") or key in _RESERVED_LABELS:
                raise MetricError(f"invalid label name {key!r}")
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise MetricError(f"the value of label {key!r} must be a string or number")
        return tuple(sorted((k, str(v)) for k, v in raw.items()))

    def families(self) -> list[tuple[str, str, str, list[tuple[str, dict[str, str], float]]]]:
        """(family name, type, help, [(sample name, labels, value), ...]) for
        each metric, as Prometheus wants them written."""
        result = []
        with self._lock:
            for name, metric in self._metrics.items():
                samples: list[tuple[str, dict[str, str], float]] = []
                if metric.kind == "counter" and not name.endswith("_total"):
                    name += "_total"  # as prometheus_client does
                for key, state in metric.series.items():
                    labels = dict(key)
                    if metric.kind in ("counter", "gauge"):
                        samples.append((name, labels, state))
                        continue
                    running = 0
                    for bound, count in zip(metric.buckets, state[2]):
                        running += count
                        samples.append((f"{name}_bucket", {**labels, "le": _bound(bound)}, running))
                    if metric.kind == "histogram":
                        samples.append((f"{name}_bucket", {**labels, "le": "+Inf"}, state[0]))
                    samples.append((f"{name}_count", labels, state[0]))
                    samples.append((f"{name}_sum", labels, state[1]))
                result.append((name, metric.kind, metric.help, samples))
        return result


def _bound(value: float) -> str:
    return repr(value)  # "1.0", like prometheus_client
