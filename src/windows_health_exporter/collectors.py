"""The collectors: one per question somebody actually asks about a client.

Every collector takes a `CommandRunner` and returns metric families. None of them catches
its own errors: a collector that cannot answer should say so by failing, and the registry
turns that into `windows_health_collector_up 0` while the rest of the scrape still
renders. Swallowing the error inside the collector would produce a metric that is quietly
wrong, which is worse than one that is visibly missing.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Protocol, cast

from windows_health_exporter.metrics import MetricFamily, Sample
from windows_health_exporter.powershell import CommandRunner, as_list
from windows_health_exporter.timestamps import TimestampError, to_unix_seconds

# Windows service names are restrictive by design. Anything outside this set is rejected
# rather than quoted, because the name goes into a PowerShell script.
SERVICE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,80}$")

SYSTEM_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$rebootKey = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\' +
    'Component Based Servicing\RebootPending'
$os = Get-CimInstance Win32_OperatingSystem
[pscustomobject]@{
    Hostname       = $env:COMPUTERNAME
    OsCaption      = $os.Caption
    OsVersion      = $os.Version
    LastBootUpTime = $os.LastBootUpTime.ToUniversalTime().ToString('o')
    PendingReboot  = [bool](Test-Path $rebootKey)
} | ConvertTo-Json -Compress
"""

DISK_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' |
    Select-Object DeviceID, Size, FreeSpace |
    ConvertTo-Json -Compress
"""

UPDATE_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$updateKey = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\' +
    'Auto Update\Results\Install'
$registryTime = (Get-ItemProperty $updateKey).LastSuccessTime
$hotfix = Get-HotFix |
    Where-Object InstalledOn |
    Sort-Object InstalledOn -Descending |
    Select-Object -First 1
$installedOn = $null
if ($hotfix) {
    $installedOn = $hotfix.InstalledOn.ToUniversalTime().ToString('o')
}
[pscustomobject]@{
    LastSuccessTime   = $registryTime
    HotFixId          = $hotfix.HotFixID
    HotFixInstalledOn = $installedOn
} | ConvertTo-Json -Compress
"""

DEFENDER_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Get-MpComputerStatus |
    Select-Object RealTimeProtectionEnabled, AMServiceEnabled, AntivirusSignatureAge |
    ConvertTo-Json -Compress
"""


class Collector(Protocol):
    """One source of metrics."""

    name: str

    def collect(self) -> Sequence[MetricFamily]:
        """Gather and return the families this collector is responsible for."""


def _require_mapping(value: object, what: str) -> dict[str, Any]:
    """Return the value as a dictionary, or say what was expected."""
    if not isinstance(value, dict):
        raise ValueError(f"Expected an object describing {what}, got {type(value).__name__}.")

    return cast("dict[str, Any]", value)


def _as_float(value: object, default: float = 0.0) -> float:
    """Read a number that PowerShell may have sent as a string, or fall back."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0

    if isinstance(value, int | float):
        return float(value)

    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default

    return default


class SystemCollector:
    """Identity, uptime and whether the machine is waiting for a restart."""

    name = "system"

    def __init__(self, runner: CommandRunner, exporter_version: str) -> None:
        """Take the runner to query through and the version to report."""
        self._runner = runner
        self._exporter_version = exporter_version

    def collect(self) -> Sequence[MetricFamily]:
        """Return the identity, boot time and pending-reboot families."""
        data = _require_mapping(self._runner.run_json(SYSTEM_SCRIPT), "the operating system")

        info = MetricFamily(
            name="windows_health_info",
            help_text="Identity of the client and the exporter, as labels on a constant 1.",
            type="gauge",
            samples=[
                Sample(
                    value=1.0,
                    labels={
                        "hostname": str(data.get("Hostname", "")),
                        "os_caption": str(data.get("OsCaption", "")),
                        "os_version": str(data.get("OsVersion", "")),
                        "exporter_version": self._exporter_version,
                    },
                )
            ],
        )

        boot_time = MetricFamily(
            name="windows_health_boot_time_seconds",
            help_text="Unix timestamp of the last boot.",
            type="gauge",
            samples=[Sample(value=to_unix_seconds(str(data["LastBootUpTime"])))],
        )

        pending_reboot = MetricFamily(
            name="windows_health_pending_reboot",
            help_text="1 when the client is waiting for a restart to finish servicing.",
            type="gauge",
            samples=[Sample(value=_as_float(data.get("PendingReboot", False)))],
        )

        return [info, boot_time, pending_reboot]


class DiskCollector:
    """Size and free space of every fixed disk."""

    name = "disk"

    def __init__(self, runner: CommandRunner) -> None:
        """Take the runner to query through."""
        self._runner = runner

    def collect(self) -> Sequence[MetricFamily]:
        """Return total and free bytes per drive letter."""
        rows = as_list(self._runner.run_json(DISK_SCRIPT))

        total_samples: list[Sample] = []
        free_samples: list[Sample] = []

        for row in rows:
            disk = _require_mapping(row, "a logical disk")
            drive = str(disk.get("DeviceID", "")).strip()
            if not drive:
                continue

            labels = {"drive": drive}
            total_samples.append(Sample(value=_as_float(disk.get("Size")), labels=labels))
            free_samples.append(Sample(value=_as_float(disk.get("FreeSpace")), labels=labels))

        return [
            MetricFamily(
                name="windows_health_disk_total_bytes",
                help_text="Size of a fixed disk in bytes.",
                type="gauge",
                samples=total_samples,
            ),
            MetricFamily(
                name="windows_health_disk_free_bytes",
                help_text="Free space on a fixed disk in bytes.",
                type="gauge",
                samples=free_samples,
            ),
        ]


class ServiceCollector:
    """Whether the services somebody cares about are installed and running."""

    name = "service"

    def __init__(self, runner: CommandRunner, service_names: Sequence[str]) -> None:
        """Take the runner and the services to watch.

        Names are validated rather than escaped. They end up inside a PowerShell script,
        and a name outside the documented character set is far more likely to be a mistake
        or an injection attempt than a real service.
        """
        for service_name in service_names:
            if not SERVICE_NAME.match(service_name):
                raise ValueError(f"'{service_name}' is not a valid Windows service name.")

        self._runner = runner
        self._service_names = list(service_names)

    def _script(self) -> str:
        """Build the query for the configured services.

        It enumerates and filters rather than passing the names to `Get-Service -Name`.
        That form writes an error for every name it cannot find, which makes PowerShell
        exit non-zero, and a service that was never installed is an expected answer here
        rather than a failed query. Enumerating a few hundred services costs a moment and
        removes the error path entirely.
        """
        quoted = ",".join(f"'{name}'" for name in self._service_names)

        return (
            "$ErrorActionPreference = 'Stop'\n"
            f"$wanted = @({quoted})\n"
            "Get-Service |\n"
            "    Where-Object { $wanted -contains $_.Name } |\n"
            "    ForEach-Object { [pscustomobject]@{ "
            "Name = $_.Name; Status = $_.Status.ToString(); "
            "StartType = $_.StartType.ToString() } } |\n"
            "    ConvertTo-Json -Compress"
        )

    def collect(self) -> Sequence[MetricFamily]:
        """Return installed and running state for each configured service."""
        if not self._service_names:
            return []

        rows = as_list(self._runner.run_json(self._script()))
        found = {
            str(_require_mapping(row, "a service").get("Name", "")).casefold(): _require_mapping(
                row, "a service"
            )
            for row in rows
        }

        installed_samples: list[Sample] = []
        running_samples: list[Sample] = []

        for service_name in self._service_names:
            labels = {"service": service_name}
            service = found.get(service_name.casefold())

            # Installed and running are separate metrics on purpose: a service that was
            # never deployed and one that crashed both read as "not running", and those
            # are different problems for whoever is on call.
            installed_samples.append(Sample(value=1.0 if service else 0.0, labels=labels))
            running = service is not None and str(service.get("Status", "")).casefold() == "running"
            running_samples.append(Sample(value=1.0 if running else 0.0, labels=labels))

        return [
            MetricFamily(
                name="windows_health_service_installed",
                help_text="1 when a watched service exists on the client.",
                type="gauge",
                samples=installed_samples,
            ),
            MetricFamily(
                name="windows_health_service_up",
                help_text="1 when a watched service is running.",
                type="gauge",
                samples=running_samples,
            ),
        ]


class UpdateCollector:
    """When the client last installed an update."""

    name = "update"

    def __init__(self, runner: CommandRunner) -> None:
        """Take the runner to query through."""
        self._runner = runner

    def collect(self) -> Sequence[MetricFamily]:
        """Return the last successful update time, if either source knows it.

        Two sources, because neither is reliable alone: the registry key is absent on
        plenty of Windows 11 installations, and the hotfix list omits updates that were
        delivered as a component update rather than a KB.

        When neither answers, the family is returned with no samples. An absent series
        means "no data" in Prometheus, which is the truth here; exporting a zero would
        claim the machine was last patched in 1970.
        """
        data = self._runner.run_json(UPDATE_SCRIPT)
        if data is None:
            return [self._family([])]

        record = _require_mapping(data, "the update history")

        for key in ("LastSuccessTime", "HotFixInstalledOn"):
            raw = record.get(key)
            if not raw:
                continue

            try:
                return [self._family([Sample(value=to_unix_seconds(str(raw)))])]
            except TimestampError:
                continue

        return [self._family([])]

    @staticmethod
    def _family(samples: Sequence[Sample]) -> MetricFamily:
        """Build the family, with or without a value."""
        return MetricFamily(
            name="windows_health_last_update_install_time_seconds",
            help_text="Unix timestamp of the last successfully installed update.",
            type="gauge",
            samples=samples,
        )


class DefenderCollector:
    """Microsoft Defender's own view of whether it is working."""

    name = "defender"

    def __init__(self, runner: CommandRunner) -> None:
        """Take the runner to query through."""
        self._runner = runner

    def collect(self) -> Sequence[MetricFamily]:
        """Return real-time protection, service state and signature age."""
        data = _require_mapping(self._runner.run_json(DEFENDER_SCRIPT), "Defender")

        return [
            MetricFamily(
                name="windows_health_defender_realtime_protection",
                help_text="1 when Defender real-time protection is on.",
                type="gauge",
                samples=[Sample(value=_as_float(data.get("RealTimeProtectionEnabled", False)))],
            ),
            MetricFamily(
                name="windows_health_defender_service_enabled",
                help_text="1 when the Defender anti-malware service is enabled.",
                type="gauge",
                samples=[Sample(value=_as_float(data.get("AMServiceEnabled", False)))],
            ),
            MetricFamily(
                name="windows_health_defender_signature_age_days",
                help_text="Age of the Defender signatures in days, as Defender reports it.",
                type="gauge",
                samples=[Sample(value=_as_float(data.get("AntivirusSignatureAge")))],
            ),
        ]
