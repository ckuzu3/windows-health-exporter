"""The command line.

Two ways to run the same collectors. `--once` prints one scrape and exits, which is what
a scheduled task writing into Prometheus' textfile collector needs, and is also the
quickest way to see what the agent would report on a machine. Without it the exporter
serves /metrics until stopped.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from windows_health_exporter import __version__
from windows_health_exporter.collectors import (
    Collector,
    DefenderCollector,
    DiskCollector,
    ServiceCollector,
    SystemCollector,
    UpdateCollector,
)
from windows_health_exporter.metrics import render
from windows_health_exporter.powershell import CommandRunner, PowerShellRunner
from windows_health_exporter.registry import CollectorRegistry
from windows_health_exporter.server import MetricsServer, create_server

# 9182 belongs to the well-known windows_exporter. Sitting next to it rather than on top
# of it means both can run on the same client.
DEFAULT_PORT = 9183
DEFAULT_HOST = "127.0.0.1"
DEFAULT_SERVICES = ("wuauserv", "WinDefend", "Schedule")

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="windows-health-exporter",
        description="Export the health of a managed Windows client as Prometheus metrics.",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="Address to bind. Defaults to localhost; a scraper elsewhere needs 0.0.0.0.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to bind.")
    parser.add_argument(
        "--service",
        action="append",
        dest="services",
        metavar="NAME",
        help="A Windows service to watch. Repeat the flag for more than one.",
    )
    parser.add_argument(
        "--powershell",
        default="powershell",
        help="The PowerShell executable to call. Use 'pwsh' for PowerShell 7.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="How many seconds to wait for a PowerShell call.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Print one scrape to stdout and exit, for a scheduled task.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Logging verbosity.",
    )
    parser.add_argument("--version", action="version", version=__version__)

    return parser


def build_collectors(runner: CommandRunner, services: Sequence[str]) -> list[Collector]:
    """Assemble the collectors in the order they should appear in a scrape."""
    return [
        SystemCollector(runner, exporter_version=__version__),
        DiskCollector(runner),
        ServiceCollector(runner, services),
        UpdateCollector(runner),
        DefenderCollector(runner),
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """Run the exporter. Returns the process exit code."""
    arguments = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, arguments.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    services = arguments.services if arguments.services is not None else list(DEFAULT_SERVICES)

    try:
        collectors = build_collectors(
            PowerShellRunner(executable=arguments.powershell, timeout_seconds=arguments.timeout),
            services,
        )
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)

        return 2

    registry = CollectorRegistry(collectors)

    if arguments.once:
        print(render(registry.gather()), end="")

        return 0

    return serve(create_server(arguments.host, arguments.port, registry))


def serve(server: MetricsServer) -> int:
    """Serve until interrupted, then close the socket. Returns the process exit code.

    Separate from `main` so a test can start it on a thread and stop it again, rather
    than the shutdown path being the one piece of the program nobody ever runs twice.
    """
    host, port = server.server_address[0], server.server_address[1]
    logger.info("Serving metrics on http://%s:%s/metrics", host, port)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping")
    finally:
        server.server_close()

    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the console script
    raise SystemExit(main())
