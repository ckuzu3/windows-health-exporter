"""Reading Windows facts through PowerShell.

Python has no portable way to ask Windows for a service state, the Defender status or the
patch history. PowerShell does, in one line each, and every Windows administrator can read
the query and tell whether it asks the right thing. So the division of labour here is:
PowerShell gathers, Python shapes and serves.

Every collector talks to the `CommandRunner` protocol rather than to `subprocess`, which
is what lets the whole test suite run on a Linux CI runner with no Windows in sight.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

# Arguments that make a PowerShell call safe to run unattended: no user profile, no
# interactive prompts, and the caller's execution policy left alone for this process only.
BASE_ARGUMENTS = ("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command")

DEFAULT_TIMEOUT_SECONDS = 15.0


class PowerShellError(RuntimeError):
    """Raised when a PowerShell call fails, times out, or returns something unreadable."""


class CommandRunner(Protocol):
    """What a collector needs in order to ask Windows a question."""

    # The shape of a JSON document is not known statically, which is what Any says here.
    def run_json(self, script: str) -> Any:  # noqa: ANN401
        """Run a script that writes JSON and return the parsed result."""


class PowerShellRunner:
    """Runs PowerShell and parses what it writes to stdout.

    The subprocess call is injected so the parsing and error handling can be tested
    without spawning anything. The real call is still exercised, by a test marked
    `windows` that runs in the Windows CI job.
    """

    def __init__(
        self,
        executable: str = "powershell",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        """Configure which interpreter to call and how long to wait for it."""
        self._executable = executable
        self._timeout_seconds = timeout_seconds
        self._run = run

    # The shape of a JSON document is not known statically, which is what Any says here.
    def run_json(self, script: str) -> Any:  # noqa: ANN401
        """Run a script and parse its JSON output.

        An empty stdout returns None rather than raising: a query that legitimately finds
        nothing, such as a service that is not installed, writes nothing at all.
        """
        try:
            completed = self._run(
                [self._executable, *BASE_ARGUMENTS, script],
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
                shell=False,
            )
        except FileNotFoundError as error:
            raise PowerShellError(f"'{self._executable}' was not found on this machine.") from error
        except subprocess.TimeoutExpired as error:
            raise PowerShellError(
                f"PowerShell did not answer within {self._timeout_seconds} seconds."
            ) from error

        if completed.returncode != 0:
            detail = (completed.stderr or "").strip() or "no error output"
            raise PowerShellError(f"PowerShell exited with {completed.returncode}: {detail}")

        output = (completed.stdout or "").strip()
        if not output:
            return None

        try:
            return json.loads(output)
        except json.JSONDecodeError as error:
            raise PowerShellError(f"PowerShell did not return JSON: {output[:200]}") from error


class FakeRunner:
    """A runner that answers from a prepared table, for tests and for a dry demonstration.

    Lookup is by substring, because a collector's script is long and a test should be able
    to key on the distinctive part of it rather than repeat it verbatim.
    """

    def __init__(self, responses: Mapping[str, Any]) -> None:
        """Map a fragment of a script to the value a run of it should return."""
        self._responses = dict(responses)
        self.calls: list[str] = []

    # The shape of a JSON document is not known statically, which is what Any says here.
    def run_json(self, script: str) -> Any:  # noqa: ANN401
        """Return the response registered for the first matching fragment."""
        self.calls.append(script)

        for fragment, response in self._responses.items():
            if fragment in script:
                if isinstance(response, Exception):
                    raise response

                return response

        raise PowerShellError(f"No fake response registered for: {script[:120]}")


def as_list(value: object) -> Sequence[Any]:
    """Normalise what ConvertTo-Json produced into a list.

    PowerShell serialises one object as an object and several as an array, so a collector
    that iterates would silently walk the keys of a single result instead of its rows.
    This is the single most common way to get Windows JSON wrong.
    """
    if value is None:
        return []

    if isinstance(value, list):
        return value

    return [value]
