"""The PowerShell boundary: argument shape, failure handling, and one real call."""

from __future__ import annotations

import json
import subprocess

import pytest

from windows_health_exporter.collectors import SYSTEM_SCRIPT
from windows_health_exporter.powershell import (
    BASE_ARGUMENTS,
    FakeRunner,
    PowerShellError,
    PowerShellRunner,
    as_list,
)


def completed(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    """Build a CompletedProcess the way subprocess.run would return one."""
    return subprocess.CompletedProcess(
        args=["powershell"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_the_call_is_not_interactive_and_ignores_the_user_profile() -> None:
    """An agent runs unattended; a profile or a prompt would hang the scrape."""
    commands: list[list[str]] = []
    options: list[dict[str, object]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        options.append(kwargs)

        return completed('{"ok":true}')

    PowerShellRunner(run=fake_run).run_json("Get-Thing")

    assert commands[0][0] == "powershell"
    assert tuple(commands[0][1:-1]) == BASE_ARGUMENTS
    assert commands[0][-1] == "Get-Thing"
    assert options[0]["shell"] is False


def test_json_output_is_parsed() -> None:
    runner = PowerShellRunner(run=lambda *_args, **_kwargs: completed('{"Name":"Spooler"}'))

    assert runner.run_json("x") == {"Name": "Spooler"}


def test_empty_output_is_no_data_rather_than_an_error() -> None:
    """A query that finds nothing, such as a service that is not installed, writes nothing."""
    runner = PowerShellRunner(run=lambda *_args, **_kwargs: completed("   \n"))

    assert runner.run_json("x") is None


def test_a_non_zero_exit_reports_what_powershell_said() -> None:
    runner = PowerShellRunner(
        run=lambda *_args, **_kwargs: completed(stderr="Access is denied.", returncode=1)
    )

    with pytest.raises(PowerShellError, match=r"exited with 1: Access is denied\."):
        runner.run_json("x")


def test_a_non_zero_exit_without_output_still_says_something_useful() -> None:
    runner = PowerShellRunner(run=lambda *_args, **_kwargs: completed(returncode=5))

    with pytest.raises(PowerShellError, match="no error output"):
        runner.run_json("x")


def test_output_that_is_not_json_is_reported_with_a_sample_of_it() -> None:
    runner = PowerShellRunner(run=lambda *_args, **_kwargs: completed("Get-Thing : not found"))

    with pytest.raises(PowerShellError, match="did not return JSON: Get-Thing"):
        runner.run_json("x")


def test_a_missing_interpreter_names_the_executable() -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError

    runner = PowerShellRunner(executable="pwsh", run=fake_run)

    with pytest.raises(PowerShellError, match="'pwsh' was not found"):
        runner.run_json("x")


def test_a_hanging_call_is_cut_off() -> None:
    """A scrape has a deadline, so a wedged query must fail rather than block it."""

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="powershell", timeout=15.0)

    runner = PowerShellRunner(timeout_seconds=15.0, run=fake_run)

    with pytest.raises(PowerShellError, match=r"did not answer within 15\.0 seconds"):
        runner.run_json("x")


@pytest.mark.parametrize(
    ("value", "expected_length"),
    [(None, 0), ({"a": 1}, 1), ([{"a": 1}, {"a": 2}], 2), ([], 0)],
)
def test_as_list_normalises_what_convert_to_json_produced(
    value: object, expected_length: int
) -> None:
    """PowerShell serialises one row as an object and several as an array."""
    assert len(as_list(value)) == expected_length


def test_as_list_does_not_walk_the_keys_of_a_single_object() -> None:
    assert as_list({"DeviceID": "C:", "Size": 10}) == [{"DeviceID": "C:", "Size": 10}]


def test_the_fake_runner_matches_on_a_fragment() -> None:
    runner = FakeRunner({"Win32_LogicalDisk": [{"DeviceID": "C:"}]})

    assert runner.run_json("Get-CimInstance Win32_LogicalDisk -Filter x") == [{"DeviceID": "C:"}]
    assert len(runner.calls) == 1


def test_the_fake_runner_can_raise_for_a_script() -> None:
    runner = FakeRunner({"Get-MpComputerStatus": PowerShellError("Defender is unavailable")})

    with pytest.raises(PowerShellError, match="Defender is unavailable"):
        runner.run_json("Get-MpComputerStatus")


def test_the_fake_runner_complains_about_an_unregistered_script() -> None:
    with pytest.raises(PowerShellError, match="No fake response registered"):
        FakeRunner({}).run_json("Get-Service")


@pytest.mark.windows
def test_the_system_script_really_runs_on_windows() -> None:
    """The one test that proves the scripts are valid PowerShell and not just plausible."""
    result = PowerShellRunner().run_json(SYSTEM_SCRIPT)

    assert isinstance(result, dict)
    assert set(result) >= {"Hostname", "OsVersion", "LastBootUpTime", "PendingReboot"}
    assert json.dumps(result)
