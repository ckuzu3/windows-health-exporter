"""Running the collectors and assembling one scrape.

The rule this module exists to enforce: one broken collector must not cost the whole
scrape. If Defender is unreachable, the disk and service metrics still have to arrive,
and the Defender failure has to be visible as a metric rather than as a 500 that a
dashboard renders as a gap.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence

from windows_health_exporter.collectors import Collector
from windows_health_exporter.metrics import MetricFamily, Sample

logger = logging.getLogger(__name__)


class CollectorRegistry:
    """Holds the collectors and produces the families for one scrape."""

    def __init__(
        self,
        collectors: Sequence[Collector],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Take the collectors to run and the clock used to time the scrape."""
        self._collectors = list(collectors)
        self._clock = clock

    def gather(self) -> list[MetricFamily]:
        """Run every collector and return all families, plus the scrape's own metrics."""
        started = self._clock()
        families: list[MetricFamily] = []
        up_samples: list[Sample] = []

        for collector in self._collectors:
            labels = {"collector": collector.name}
            try:
                families.extend(collector.collect())
            except Exception:
                # Broad on purpose. A collector reaches into Windows through a subprocess
                # and can fail in ways this module cannot enumerate; what matters is that
                # the failure becomes a metric instead of an empty scrape.
                logger.warning("Collector '%s' failed", collector.name, exc_info=True)
                up_samples.append(Sample(value=0.0, labels=labels))
                continue

            up_samples.append(Sample(value=1.0, labels=labels))

        duration = max(0.0, self._clock() - started)

        return [
            MetricFamily(
                name="windows_health_up",
                help_text="1 when the exporter itself answered the scrape.",
                type="gauge",
                samples=[Sample(value=1.0)],
            ),
            MetricFamily(
                name="windows_health_collector_up",
                help_text="1 when a collector gathered its metrics without failing.",
                type="gauge",
                samples=up_samples,
            ),
            MetricFamily(
                name="windows_health_scrape_duration_seconds",
                help_text="How long the last scrape took.",
                type="gauge",
                samples=[Sample(value=duration)],
            ),
            *families,
        ]
