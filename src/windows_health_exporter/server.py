"""The HTTP endpoint Prometheus scrapes.

`http.server` rather than a framework, because this agent runs on managed clients and the
one thing it must not need is a package installation. Two routes, no templates, no state.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from windows_health_exporter.metrics import render
from windows_health_exporter.registry import CollectorRegistry

logger = logging.getLogger(__name__)

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
METRICS_PATH = "/metrics"
HEALTH_PATH = "/healthz"


class _Handler(BaseHTTPRequestHandler):
    """Serves /metrics and /healthz and nothing else."""

    server_version = "windows-health-exporter"
    sys_version = ""

    @property
    def _registry(self) -> CollectorRegistry:
        """The registry the server was built with."""
        registry: CollectorRegistry = self.server.registry  # type: ignore[attr-defined]

        return registry

    def _respond(self, status: HTTPStatus, body: str, content_type: str = CONTENT_TYPE) -> None:
        """Send one response, headers and body."""
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        """Route a GET request."""
        path = self.path.split("?", 1)[0]

        if path == HEALTH_PATH:
            self._respond(HTTPStatus.OK, "ok\n", "text/plain; charset=utf-8")
            return

        if path != METRICS_PATH:
            self._respond(
                HTTPStatus.NOT_FOUND,
                f"Not found. Try {METRICS_PATH} or {HEALTH_PATH}.\n",
                "text/plain; charset=utf-8",
            )
            return

        try:
            body = render(self._registry.gather())
        except Exception:
            # The registry already contains every collector failure it knows about, so
            # reaching this means the exporter itself is broken. Prometheus should see
            # that as a failed scrape, not as a page of plausible-looking zeros.
            logger.exception("Scrape failed")
            self._respond(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "scrape failed\n",
                "text/plain; charset=utf-8",
            )
            return

        self._respond(HTTPStatus.OK, body)

    def log_message(self, format: str, *args: object) -> None:
        """Send request logs to the logging module instead of straight to stderr."""
        logger.debug("%s - %s", self.address_string(), format % args)


class MetricsServer(ThreadingHTTPServer):
    """An HTTP server that knows which registry to scrape."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], registry: CollectorRegistry) -> None:
        """Bind the socket and remember the registry."""
        super().__init__(address, _Handler)
        self.registry = registry


def create_server(host: str, port: int, registry: CollectorRegistry) -> MetricsServer:
    """Create a server bound to the given address.

    Port 0 asks the operating system for a free port, which is how the tests avoid
    fighting over a fixed one.
    """
    return MetricsServer((host, port), registry)
