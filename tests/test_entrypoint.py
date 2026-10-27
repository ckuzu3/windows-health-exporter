"""The parts that only matter when the program is actually started."""

from __future__ import annotations

import subprocess
import sys
import threading
import urllib.request

from windows_health_exporter import __version__
from windows_health_exporter.cli import serve
from windows_health_exporter.registry import CollectorRegistry
from windows_health_exporter.server import create_server


def test_the_module_can_be_run_with_dash_m() -> None:
    """`python -m windows_health_exporter` is how a scheduled task will call it."""
    completed = subprocess.run(
        [sys.executable, "-m", "windows_health_exporter", "--version"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0
    assert __version__ in completed.stdout + completed.stderr


def test_serve_runs_until_it_is_shut_down_and_closes_the_socket() -> None:
    """The shutdown path is otherwise the one piece of the program nobody runs twice."""
    server = create_server("127.0.0.1", 0, CollectorRegistry([]))
    port = server.server_address[1]
    result: list[int] = []

    thread = threading.Thread(target=lambda: result.append(serve(server)), daemon=True)
    thread.start()

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as response:
        assert response.status == 200

    server.shutdown()
    thread.join(timeout=5)

    assert result == [0]
    assert thread.is_alive() is False


class InterruptingServer:
    """A server that reacts to Ctrl+C the way the real one does."""

    def __init__(self) -> None:
        self.server_address = ("127.0.0.1", 0)
        self.closed = False

    def serve_forever(self) -> None:
        raise KeyboardInterrupt

    def server_close(self) -> None:
        self.closed = True


def test_ctrl_c_stops_cleanly_and_still_closes_the_socket() -> None:
    """A half-closed socket would keep the port busy until the machine is rebooted."""
    server = InterruptingServer()

    exit_code = serve(server)  # type: ignore[arg-type]

    assert exit_code == 0
    assert server.closed is True
