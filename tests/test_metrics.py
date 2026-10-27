"""The exposition format: escaping, numbers, and the shape of a document."""

from __future__ import annotations

import math

import pytest

from windows_health_exporter.metrics import MetricFamily, Sample, format_value, render


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.0, "1"),
        (0.0, "0"),
        (-3.0, "-3"),
        (1759000000.0, "1759000000"),
        (0.5, "0.5"),
        (1.25, "1.25"),
    ],
)
def test_whole_numbers_lose_the_trailing_zero(value: float, expected: str) -> None:
    assert format_value(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (math.inf, "+Inf"),
        (-math.inf, "-Inf"),
        (math.nan, "NaN"),
    ],
)
def test_the_special_values_use_the_format_spelling(value: float, expected: str) -> None:
    """Python writes 'inf' and 'nan'; Prometheus would reject both."""
    assert format_value(value) == expected


def test_a_family_renders_help_and_type_once() -> None:
    document = render(
        [
            MetricFamily(
                name="windows_health_pending_reboot",
                help_text="1 when a restart is pending.",
                type="gauge",
                samples=[Sample(value=1.0)],
            )
        ]
    )

    assert document == (
        "# HELP windows_health_pending_reboot 1 when a restart is pending.\n"
        "# TYPE windows_health_pending_reboot gauge\n"
        "windows_health_pending_reboot 1\n"
    )


def test_labels_are_sorted_so_a_scrape_is_stable() -> None:
    document = render(
        [
            MetricFamily(
                name="windows_health_info",
                help_text="Identity.",
                type="gauge",
                samples=[Sample(value=1.0, labels={"os_version": "10.0", "hostname": "ws-0001"})],
            )
        ]
    )

    assert 'windows_health_info{hostname="ws-0001",os_version="10.0"} 1' in document


def test_a_label_value_cannot_break_out_of_its_quotes() -> None:
    """A drive label or a machine name is not under this exporter's control."""
    document = render(
        [
            MetricFamily(
                name="windows_health_info",
                help_text="Identity.",
                type="gauge",
                samples=[Sample(value=1.0, labels={"hostname": 'we"ird\\host\nname'})],
            )
        ]
    )

    assert r'hostname="we\"ird\\host\nname"' in document
    assert document.count("\n") == 3


def test_a_newline_in_help_is_escaped_rather_than_ending_the_line() -> None:
    document = render([MetricFamily(name="m", help_text="first\nsecond", type="gauge", samples=[])])

    assert document == "# HELP m first\\nsecond\n# TYPE m gauge\n"


def test_a_family_without_samples_still_announces_itself() -> None:
    """A metric with no data is not the same as a metric that disappeared."""
    document = render(
        [
            MetricFamily(
                name="windows_health_last_update_install_time_seconds",
                help_text="Unix timestamp of the last update.",
                type="gauge",
                samples=[],
            )
        ]
    )

    lines = document.splitlines()

    assert "# TYPE windows_health_last_update_install_time_seconds gauge" in lines
    # A sample is any line that is not a comment, and there should be none.
    assert [line for line in lines if line and not line.startswith("#")] == []


def test_several_samples_share_one_header() -> None:
    document = render(
        [
            MetricFamily(
                name="windows_health_disk_free_bytes",
                help_text="Free space.",
                type="gauge",
                samples=[
                    Sample(value=1.0, labels={"drive": "C:"}),
                    Sample(value=2.0, labels={"drive": "D:"}),
                ],
            )
        ]
    )

    assert document.count("# TYPE") == 1
    assert document.count("windows_health_disk_free_bytes{") == 2


def test_an_empty_document_is_still_a_document() -> None:
    assert render([]) == "\n"


def test_with_samples_leaves_the_original_untouched() -> None:
    family = MetricFamily(name="m", help_text="h", type="gauge", samples=[])

    replaced = family.with_samples([Sample(value=1.0)])

    assert len(family.samples) == 0
    assert len(replaced.samples) == 1
    assert replaced.name == "m"
