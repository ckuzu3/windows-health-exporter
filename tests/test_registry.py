"""The registry: one broken collector must not cost the whole scrape."""

from __future__ import annotations

from collections.abc import Sequence

from windows_health_exporter.metrics import MetricFamily, Sample
from windows_health_exporter.powershell import PowerShellError
from windows_health_exporter.registry import CollectorRegistry


class StubCollector:
    """A collector that returns a fixed family."""

    def __init__(self, name: str, metric_name: str) -> None:
        self.name = name
        self._metric_name = metric_name

    def collect(self) -> Sequence[MetricFamily]:
        return [
            MetricFamily(
                name=self._metric_name, help_text="h", type="gauge", samples=[Sample(value=1.0)]
            )
        ]


class BrokenCollector:
    """A collector that always fails, the way a locked-down Defender query would."""

    name = "defender"

    def collect(self) -> Sequence[MetricFamily]:
        raise PowerShellError("Access is denied.")


def sample_for(families: list[MetricFamily], name: str, label_value: str) -> float:
    """Find one labelled sample by family name and collector label."""
    for family in families:
        if family.name != name:
            continue
        for sample in family.samples:
            if sample.labels.get("collector") == label_value:
                return sample.value

    raise AssertionError(f"No sample {name}{{collector={label_value}}}")


def test_every_scrape_reports_that_the_exporter_answered() -> None:
    families = CollectorRegistry([]).gather()

    up = next(f for f in families if f.name == "windows_health_up")

    assert up.samples[0].value == 1.0


def test_a_working_collector_reports_up() -> None:
    families = CollectorRegistry([StubCollector("disk", "windows_health_disk_free_bytes")]).gather()

    assert sample_for(families, "windows_health_collector_up", "disk") == 1.0


def test_a_failing_collector_reports_down_without_taking_the_rest_with_it() -> None:
    registry = CollectorRegistry(
        [
            StubCollector("disk", "windows_health_disk_free_bytes"),
            BrokenCollector(),
            StubCollector("service", "windows_health_service_up"),
        ]
    )

    families = registry.gather()
    names = {family.name for family in families}

    assert sample_for(families, "windows_health_collector_up", "defender") == 0.0
    assert sample_for(families, "windows_health_collector_up", "disk") == 1.0
    assert "windows_health_disk_free_bytes" in names
    assert "windows_health_service_up" in names


def test_the_scrape_reports_how_long_it_took() -> None:
    ticks = iter([10.0, 10.25])
    registry = CollectorRegistry([], clock=lambda: next(ticks))

    families = registry.gather()
    duration = next(f for f in families if f.name == "windows_health_scrape_duration_seconds")

    assert duration.samples[0].value == 0.25


def test_a_clock_that_jumps_backwards_does_not_produce_a_negative_duration() -> None:
    """Rare, but a negative duration in a dashboard is a bug report nobody can act on."""
    ticks = iter([10.0, 9.0])
    registry = CollectorRegistry([], clock=lambda: next(ticks))

    families = registry.gather()
    duration = next(f for f in families if f.name == "windows_health_scrape_duration_seconds")

    assert duration.samples[0].value == 0.0


def test_the_meta_metrics_come_first() -> None:
    """They are what a reader checks before trusting anything below them."""
    families = CollectorRegistry([StubCollector("disk", "windows_health_disk_free_bytes")]).gather()

    assert [family.name for family in families[:3]] == [
        "windows_health_up",
        "windows_health_collector_up",
        "windows_health_scrape_duration_seconds",
    ]
