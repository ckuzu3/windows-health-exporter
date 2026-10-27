"""The Prometheus text exposition format.

Rendering is a pure function over plain data, with no collector and no HTTP anywhere near
it. That is what makes the fiddly parts — escaping, the one HELP and TYPE line per family,
the number formatting — testable as a handful of string comparisons.

The format is small enough that implementing it is a page of code. Pulling in
prometheus_client would have meant a runtime dependency on every managed client this
agent is copied to, which is the one thing this project is trying not to need.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

MetricType = Literal["gauge", "counter"]


@dataclass(frozen=True)
class Sample:
    """One measured value, with the labels that identify it."""

    value: float
    labels: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MetricFamily:
    """One metric name, its documentation, and every sample under it."""

    name: str
    help_text: str
    type: MetricType
    samples: Sequence[Sample] = ()

    def with_samples(self, samples: Sequence[Sample]) -> MetricFamily:
        """Return a copy carrying different samples."""
        return MetricFamily(
            name=self.name, help_text=self.help_text, type=self.type, samples=samples
        )


def _escape_help(text: str) -> str:
    """Escape a HELP line: only the backslash and the newline carry meaning there."""
    return text.replace("\\", r"\\").replace("\n", r"\n")


def _escape_label_value(value: str) -> str:
    """Escape a label value: the backslash, the double quote and the newline."""
    return value.replace("\\", r"\\").replace('"', r"\"").replace("\n", r"\n")


def format_value(value: float) -> str:
    """Render a number the way Prometheus expects it.

    Whole numbers lose the trailing '.0', because a file full of '1.0' is harder to read
    than one full of '1'. The three special values have their own spelling in the format
    and would otherwise be rendered as Python's 'inf' and 'nan'.
    """
    if math.isnan(value):
        return "NaN"

    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"

    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))

    return repr(float(value))


def _render_labels(labels: Mapping[str, str]) -> str:
    """Render a label set, sorted so the output is stable between scrapes."""
    if not labels:
        return ""

    pairs = ",".join(
        f'{name}="{_escape_label_value(value)}"' for name, value in sorted(labels.items())
    )

    return "{" + pairs + "}"


def render(families: Sequence[MetricFamily]) -> str:
    """Render metric families as an exposition document.

    A family with no samples still gets its HELP and TYPE lines. That is deliberate: a
    scraper then sees that the metric exists and has no data, which is different from the
    metric having disappeared.
    """
    lines: list[str] = []

    for family in families:
        lines.append(f"# HELP {family.name} {_escape_help(family.help_text)}")
        lines.append(f"# TYPE {family.name} {family.type}")

        for sample in family.samples:
            lines.append(
                f"{family.name}{_render_labels(sample.labels)} {format_value(sample.value)}"
            )

    return "\n".join(lines) + "\n"
