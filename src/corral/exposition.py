"""A parser for the Prometheus text exposition format (version 0.0.4).

Only what a scrape needs: sample lines, and the ``# TYPE`` / ``# HELP``
comments that say which family a sample belongs to and what it measures.
Other comments are skipped, as are samples whose value is not a finite number
(they cannot be stored or charted).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
_ESCAPES = {"\\": "\\", '"': '"', "n": "\n"}

# The suffixes each family type adds to its name, and what a sample with that
# suffix is: a counter, a gauge, or a histogram bucket (a counter with ``le``).
_SUFFIXES = {
    "counter": {"": "counter", "_total": "counter", "_created": "gauge"},
    "gauge": {"": "gauge"},
    "histogram": {"_bucket": "bucket", "_sum": "counter", "_count": "counter", "_created": "gauge"},
    "summary": {"": "gauge", "_sum": "counter", "_count": "counter", "_created": "gauge"},
}


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class Sample:
    name: str
    labels: tuple[tuple[str, str], ...]  # sorted by label name
    value: float
    family: str
    kind: str  # "counter", "gauge", "bucket" or "untyped"
    help: str


def _labels(text: str, pos: int, lineno: int) -> tuple[dict[str, str], int]:
    """Parse ``{a="x",b="y"}`` starting at the ``{`` at ``pos``."""
    labels: dict[str, str] = {}
    pos += 1
    while True:
        while pos < len(text) and text[pos] in " \t":
            pos += 1
        if pos < len(text) and text[pos] == "}":
            return labels, pos + 1
        m = _LABEL_NAME.match(text, pos)
        if not m:
            raise ParseError(f"line {lineno}: bad label name")
        name, pos = m.group(), m.end()
        if text[pos : pos + 2] != '="':
            raise ParseError(f"line {lineno}: expected =\" after label {name}")
        pos += 2
        value = []
        while True:
            if pos >= len(text):
                raise ParseError(f"line {lineno}: unterminated label value")
            ch = text[pos]
            if ch == '"':
                pos += 1
                break
            if ch == "\\" and pos + 1 < len(text):
                value.append(_ESCAPES.get(text[pos + 1], "\\" + text[pos + 1]))
                pos += 2
            else:
                value.append(ch)
                pos += 1
        if name in labels:
            raise ParseError(f"line {lineno}: label {name} given twice")
        labels[name] = "".join(value)
        while pos < len(text) and text[pos] in " \t":
            pos += 1
        if pos < len(text) and text[pos] == ",":
            pos += 1
        elif pos < len(text) and text[pos] == "}":
            return labels, pos + 1
        else:
            raise ParseError(f"line {lineno}: expected , or }} in labels")


def _value(text: str, lineno: int) -> float:
    try:
        return float(text)  # also accepts NaN, +Inf, -Inf
    except ValueError:
        raise ParseError(f"line {lineno}: bad value {text!r}") from None


def parse(text: str) -> list[Sample]:
    types: dict[str, str] = {}
    helps: dict[str, str] = {}
    samples: list[Sample] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            parts = line.split(None, 3)
            if len(parts) >= 3 and parts[1] == "TYPE":
                types[parts[2]] = parts[3].strip() if len(parts) > 3 else "untyped"
            elif len(parts) >= 3 and parts[1] == "HELP":
                helps[parts[2]] = (parts[3] if len(parts) > 3 else "").replace("\\n", "\n").replace("\\\\", "\\")
            continue
        m = _NAME.match(line)
        if not m:
            raise ParseError(f"line {lineno}: bad metric name")
        name, pos = m.group(), m.end()
        labels: dict[str, str] = {}
        if pos < len(line) and line[pos] == "{":
            labels, pos = _labels(line, pos, lineno)
        rest = line[pos:].split()
        if not rest or len(rest) > 2:
            raise ParseError(f"line {lineno}: expected a value after {name}")
        value = _value(rest[0], lineno)
        if not math.isfinite(value):
            continue
        family, kind = _family(name, types)
        samples.append(
            Sample(name, tuple(sorted(labels.items())), value, family, kind, helps.get(family, ""))
        )
    return samples


def _family(name: str, types: dict[str, str]) -> tuple[str, str]:
    for suffix in ("", "_total", "_bucket", "_sum", "_count", "_created"):
        if suffix and not name.endswith(suffix):
            continue
        family = name[: len(name) - len(suffix)] if suffix else name
        kind = _SUFFIXES.get(types.get(family, ""), {}).get(suffix)
        if kind:
            return family, kind
    return name, "untyped"
