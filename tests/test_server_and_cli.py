"""The HTTP endpoint and the command line, both driven for real."""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence

import pytest

from windows_health_exporter.cli import build_collectors, build_parser, main
from windows_health_exporter.metrics import MetricFamily, Sample
from windows_health_exporter.powershell import FakeRunner
from windows_health_exporter.registry import CollectorRegistry
from windows_health_exporter.server import create_server


class StubCollector:
    """A collector with one predictable metric."""

    name = "stub"

    def collect(self) -> Sequence[MetricFamily]:
        return [
            MetricFamily(
                name="windows_health_pending_reboot",
                help_text="1 when a restart is pending.",
                type="gauge",
                samples=[Sample(value=1.0)],
            )
        ]


class ExplodingRegistry(CollectorRegistry):
    """A registry whose own gathering fails, which is the exporter being broken."""

    def gather(self) -> list[MetricFamily]:
        raise RuntimeError("the exporter itself is broken")


@pytest.fixture
def base_url() -> Iterator[str]:
    """Serve metrics on a port the operating system picks, and stop again afterwards."""
    server = create_server("127.0.0.1", 0, CollectorRegistry([StubCollector()]))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def fetch(url: str) -> tuple[int, str, str]:
    """Fetch a URL and return status, body and content type, errors included."""
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - a localhost test
            return response.status, response.read().decode(), response.headers["Content-Type"]
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(), error.headers["Content-Type"]


def test_metrics_are_served_in_the_exposition_format(base_url: str) -> None:
    status, body, content_type = fetch(f"{base_url}/metrics")

    assert status == 200
    assert content_type.startswith("text/plain")
    assert "version=0.0.4" in content_type
    assert "# TYPE windows_health_pending_reboot gauge" in body
    assert body.endswith("\n")


def test_the_scrape_includes_the_exporter_own_metrics(base_url: str) -> None:
    _status, body, _content_type = fetch(f"{base_url}/metrics")

    assert "windows_health_up 1" in body
    assert 'windows_health_collector_up{collector="stub"} 1' in body


def test_a_query_string_does_not_confuse_the_route(base_url: str) -> None:
    status, _body, _content_type = fetch(f"{base_url}/metrics?debug=1")

    assert status == 200


def test_healthz_answers_without_scraping(base_url: str) -> None:
    status, body, _content_type = fetch(f"{base_url}/healthz")

    assert status == 200
    assert body == "ok\n"


def test_an_unknown_path_says_where_to_look(base_url: str) -> None:
    status, body, _content_type = fetch(f"{base_url}/")

    assert status == 404
    assert "/metrics" in body


def test_a_broken_exporter_fails_the_scrape_instead_of_serving_zeros() -> None:
    """Prometheus must see a failed scrape, not a page of plausible-looking numbers."""
    server = create_server("127.0.0.1", 0, ExplodingRegistry([]))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        status, body, _content_type = fetch(f"http://127.0.0.1:{server.server_address[1]}/metrics")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 500
    assert body == "scrape failed\n"


def test_the_parser_defaults_to_localhost_and_a_free_port() -> None:
    """Binding everywhere by default would expose a client's state to the network."""
    arguments = build_parser().parse_args([])

    assert arguments.host == "127.0.0.1"
    assert arguments.port == 9183
    assert arguments.services is None


def test_services_can_be_repeated() -> None:
    arguments = build_parser().parse_args(["--service", "wuauserv", "--service", "WinDefend"])

    assert arguments.services == ["wuauserv", "WinDefend"]


def test_build_collectors_wires_every_source() -> None:
    collectors = build_collectors(FakeRunner({}), ["wuauserv"])

    assert [collector.name for collector in collectors] == [
        "system",
        "disk",
        "service",
        "update",
        "defender",
    ]


def test_once_prints_a_scrape_and_exits(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["--once", "--service", "wuauserv", "--powershell", "definitely-not-a-shell"])
    output = capsys.readouterr().out

    # Every collector fails because there is no such interpreter, and that is the point:
    # the scrape still renders, and says which collectors did not answer.
    assert exit_code == 0
    assert "windows_health_up 1" in output
    assert 'windows_health_collector_up{collector="defender"} 0' in output


def test_an_invalid_service_name_exits_with_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["--once", "--service", "not a service"])

    assert exit_code == 2
    assert "not a valid Windows service name" in capsys.readouterr().err
