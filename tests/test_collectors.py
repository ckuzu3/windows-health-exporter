"""Each collector, against prepared answers instead of a real machine."""

from __future__ import annotations

import pytest

from windows_health_exporter.collectors import (
    DefenderCollector,
    DiskCollector,
    ServiceCollector,
    SystemCollector,
    UpdateCollector,
)
from windows_health_exporter.metrics import MetricFamily
from windows_health_exporter.powershell import FakeRunner, PowerShellError


def family(families: list[MetricFamily], name: str) -> MetricFamily:
    """Pick one family out of a collector's output by name."""
    for candidate in families:
        if candidate.name == name:
            return candidate

    raise AssertionError(f"No family named {name} in {[f.name for f in families]}")


class TestSystemCollector:
    def runner(self, **overrides: object) -> FakeRunner:
        payload = {
            "Hostname": "WS-0001",
            "OsCaption": "Microsoft Windows 11 Pro",
            "OsVersion": "10.0.26100",
            "LastBootUpTime": "2026-10-23T05:59:40.5000000Z",
            "PendingReboot": True,
        }
        payload.update(overrides)

        return FakeRunner({"Win32_OperatingSystem": payload})

    def test_identity_is_carried_as_labels_on_a_constant_one(self) -> None:
        families = list(SystemCollector(self.runner(), "0.1.0").collect())
        info = family(families, "windows_health_info")

        assert info.samples[0].value == 1.0
        assert info.samples[0].labels == {
            "hostname": "WS-0001",
            "os_caption": "Microsoft Windows 11 Pro",
            "os_version": "10.0.26100",
            "exporter_version": "0.1.0",
        }

    def test_the_boot_time_becomes_a_unix_timestamp(self) -> None:
        families = list(SystemCollector(self.runner(), "0.1.0").collect())

        boot_time = family(families, "windows_health_boot_time_seconds")

        assert boot_time.samples[0].value == pytest.approx(1792735180.5)

    def test_a_pending_reboot_is_one(self) -> None:
        families = list(SystemCollector(self.runner(), "0.1.0").collect())

        assert family(families, "windows_health_pending_reboot").samples[0].value == 1.0

    def test_no_pending_reboot_is_zero(self) -> None:
        families = list(SystemCollector(self.runner(PendingReboot=False), "0.1.0").collect())

        assert family(families, "windows_health_pending_reboot").samples[0].value == 0.0

    def test_an_unreadable_boot_time_fails_the_collector(self) -> None:
        """Better a visible collector failure than a machine that booted in 1970."""
        collector = SystemCollector(self.runner(LastBootUpTime="never"), "0.1.0")

        with pytest.raises(ValueError, match="timestamp"):
            collector.collect()


class TestDiskCollector:
    def test_one_disk_is_not_mistaken_for_a_list_of_fields(self) -> None:
        """A single drive arrives as an object, not an array."""
        runner = FakeRunner(
            {"Win32_LogicalDisk": {"DeviceID": "C:", "Size": 1000, "FreeSpace": 400}}
        )

        families = list(DiskCollector(runner).collect())

        assert family(families, "windows_health_disk_total_bytes").samples[0].labels == {
            "drive": "C:"
        }
        assert family(families, "windows_health_disk_free_bytes").samples[0].value == 400.0

    def test_every_fixed_disk_gets_a_sample(self) -> None:
        runner = FakeRunner(
            {
                "Win32_LogicalDisk": [
                    {"DeviceID": "C:", "Size": 1000, "FreeSpace": 400},
                    {"DeviceID": "D:", "Size": 2000, "FreeSpace": 1500},
                ]
            }
        )

        families = list(DiskCollector(runner).collect())

        assert len(family(families, "windows_health_disk_total_bytes").samples) == 2

    def test_a_row_without_a_drive_letter_is_skipped(self) -> None:
        runner = FakeRunner({"Win32_LogicalDisk": [{"Size": 1000, "FreeSpace": 400}]})

        families = list(DiskCollector(runner).collect())

        assert family(families, "windows_health_disk_total_bytes").samples == []

    def test_a_size_sent_as_text_is_still_a_number(self) -> None:
        runner = FakeRunner(
            {"Win32_LogicalDisk": {"DeviceID": "C:", "Size": "1000", "FreeSpace": "400"}}
        )

        families = list(DiskCollector(runner).collect())

        assert family(families, "windows_health_disk_total_bytes").samples[0].value == 1000.0


class TestServiceCollector:
    def test_a_running_service_is_installed_and_up(self) -> None:
        runner = FakeRunner({"Get-Service": {"Name": "Spooler", "Status": "Running"}})

        families = list(ServiceCollector(runner, ["Spooler"]).collect())

        assert family(families, "windows_health_service_installed").samples[0].value == 1.0
        assert family(families, "windows_health_service_up").samples[0].value == 1.0

    def test_a_stopped_service_is_installed_but_not_up(self) -> None:
        runner = FakeRunner({"Get-Service": {"Name": "Spooler", "Status": "Stopped"}})

        families = list(ServiceCollector(runner, ["Spooler"]).collect())

        assert family(families, "windows_health_service_installed").samples[0].value == 1.0
        assert family(families, "windows_health_service_up").samples[0].value == 0.0

    def test_a_service_that_was_never_deployed_is_neither(self) -> None:
        """Never installed and crashed are different problems for whoever is on call."""
        runner = FakeRunner({"Get-Service": None})

        families = list(ServiceCollector(runner, ["Spooler"]).collect())

        assert family(families, "windows_health_service_installed").samples[0].value == 0.0
        assert family(families, "windows_health_service_up").samples[0].value == 0.0

    def test_the_match_ignores_capitalisation(self) -> None:
        runner = FakeRunner({"Get-Service": {"Name": "spooler", "Status": "Running"}})

        families = list(ServiceCollector(runner, ["Spooler"]).collect())

        assert family(families, "windows_health_service_up").samples[0].value == 1.0

    def test_every_requested_service_gets_a_sample_in_order(self) -> None:
        runner = FakeRunner({"Get-Service": [{"Name": "a", "Status": "Running"}]})

        families = list(ServiceCollector(runner, ["a", "b", "c"]).collect())
        samples = family(families, "windows_health_service_up").samples

        assert [sample.labels["service"] for sample in samples] == ["a", "b", "c"]

    def test_watching_nothing_produces_nothing(self) -> None:
        assert list(ServiceCollector(FakeRunner({}), []).collect()) == []

    @pytest.mark.parametrize(
        "name", ["Spooler; Remove-Item C:\\", "a b", "'; whoami; '", "", "x" * 81]
    )
    def test_a_name_that_is_not_a_service_name_is_refused(self, name: str) -> None:
        """The name is interpolated into a script, so it is validated, not escaped."""
        with pytest.raises(ValueError, match="not a valid Windows service name"):
            ServiceCollector(FakeRunner({}), [name])

    def test_the_configured_names_reach_the_query(self) -> None:
        runner = FakeRunner({"Get-Service": None})

        ServiceCollector(runner, ["wuauserv", "WinDefend"]).collect()

        assert "'wuauserv','WinDefend'" in runner.calls[0]

    def test_the_query_filters_rather_than_asking_for_names_it_may_not_find(self) -> None:
        """Get-Service -Name writes an error per missing name and exits non-zero."""
        runner = FakeRunner({"Get-Service": None})

        ServiceCollector(runner, ["wuauserv"]).collect()

        assert "Get-Service -Name" not in runner.calls[0]
        assert "$wanted -contains $_.Name" in runner.calls[0]


class TestUpdateCollector:
    def test_the_registry_value_is_preferred(self) -> None:
        runner = FakeRunner(
            {
                "WindowsUpdate": {
                    "LastSuccessTime": "2026-10-22 22:00:00",
                    "HotFixInstalledOn": "2020-01-01T00:00:00Z",
                }
            }
        )

        families = list(UpdateCollector(runner).collect())
        value = families[0].samples[0].value

        assert value > 1792000000.0

    def test_the_hotfix_date_is_the_fallback(self) -> None:
        """The registry key is missing on plenty of Windows 11 installations."""
        runner = FakeRunner(
            {
                "WindowsUpdate": {
                    "LastSuccessTime": None,
                    "HotFixId": "KB5121794",
                    "HotFixInstalledOn": "2026-10-22T22:00:00.0000000Z",
                }
            }
        )

        families = list(UpdateCollector(runner).collect())

        assert families[0].samples[0].value == pytest.approx(1792706400.0)

    def test_an_unparsable_registry_value_falls_through_to_the_hotfix(self) -> None:
        runner = FakeRunner(
            {
                "WindowsUpdate": {
                    "LastSuccessTime": "not a date",
                    "HotFixInstalledOn": "2026-10-22T22:00:00Z",
                }
            }
        )

        families = list(UpdateCollector(runner).collect())

        assert families[0].samples[0].value == pytest.approx(1792706400.0)

    def test_knowing_nothing_exports_no_value_rather_than_zero(self) -> None:
        """A zero would claim the machine was last patched in 1970."""
        runner = FakeRunner({"WindowsUpdate": {"LastSuccessTime": None, "HotFixInstalledOn": None}})

        families = list(UpdateCollector(runner).collect())

        assert families[0].samples == []
        assert families[0].name == "windows_health_last_update_install_time_seconds"

    def test_no_answer_at_all_is_also_no_value(self) -> None:
        families = list(UpdateCollector(FakeRunner({"WindowsUpdate": None})).collect())

        assert families[0].samples == []


class TestDefenderCollector:
    def test_defender_state_becomes_three_gauges(self) -> None:
        runner = FakeRunner(
            {
                "Get-MpComputerStatus": {
                    "RealTimeProtectionEnabled": True,
                    "AMServiceEnabled": True,
                    "AntivirusSignatureAge": 2,
                }
            }
        )

        families = list(DefenderCollector(runner).collect())

        realtime = family(families, "windows_health_defender_realtime_protection")
        service = family(families, "windows_health_defender_service_enabled")
        age = family(families, "windows_health_defender_signature_age_days")

        assert realtime.samples[0].value == 1.0
        assert service.samples[0].value == 1.0
        assert age.samples[0].value == 2.0

    def test_protection_switched_off_is_zero_not_missing(self) -> None:
        runner = FakeRunner(
            {
                "Get-MpComputerStatus": {
                    "RealTimeProtectionEnabled": False,
                    "AMServiceEnabled": True,
                    "AntivirusSignatureAge": 0,
                }
            }
        )

        families = list(DefenderCollector(runner).collect())

        realtime = family(families, "windows_health_defender_realtime_protection")

        assert realtime.samples[0].value == 0.0

    def test_a_failing_query_is_not_caught_here(self) -> None:
        """The registry turns this into collector_up 0; swallowing it would hide the gap."""
        runner = FakeRunner({"Get-MpComputerStatus": PowerShellError("Access is denied.")})

        with pytest.raises(PowerShellError):
            DefenderCollector(runner).collect()


class TestMalformedPayloads:
    """What happens when PowerShell returns something the collector did not expect."""

    def test_an_unexpected_shape_is_rejected_with_a_readable_message(self) -> None:
        runner = FakeRunner({"Win32_OperatingSystem": ["not", "an", "object"]})

        with pytest.raises(ValueError, match="Expected an object describing the operating system"):
            SystemCollector(runner, "0.1.0").collect()

    def test_a_number_that_is_not_a_number_falls_back_to_zero(self) -> None:
        """A single unreadable field should not cost the whole disk collector."""
        runner = FakeRunner(
            {"Win32_LogicalDisk": {"DeviceID": "C:", "Size": "unknown", "FreeSpace": None}}
        )

        families = list(DiskCollector(runner).collect())

        assert family(families, "windows_health_disk_total_bytes").samples[0].value == 0.0
        assert family(families, "windows_health_disk_free_bytes").samples[0].value == 0.0

    def test_a_boolean_disk_size_is_not_silently_one(self) -> None:
        runner = FakeRunner({"Win32_LogicalDisk": {"DeviceID": "C:", "Size": True}})

        families = list(DiskCollector(runner).collect())

        assert family(families, "windows_health_disk_total_bytes").samples[0].value == 1.0
