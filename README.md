# windows-health-exporter

A small agent that reports the health of a managed Windows client as Prometheus metrics:
identity and uptime, disk space, watched services, patch level and Defender state.

It has no runtime dependencies. Python 3.11 and the standard library are enough.

## Problem

Two questions keep coming back in support work, and neither is answered by looking at one
machine: which clients stopped checking in, and which ones are in a state nobody would
sign off — Defender off, no patch in two months, a restart pending since forever, a disk
that is about to make the next update fail.

The data exists on every client. Collecting it by hand means a remote session per machine
and a spreadsheet that is stale the next morning. What is missing is the client telling a
monitoring system about itself, continuously, in a form that can be alerted on.

## Solution

An exporter in the shape Prometheus expects, built around two decisions.

**No runtime dependencies.** This agent is meant to be copied onto managed clients. A
`pip install` on several hundred of them is a deployment problem rather than a detail, so
everything it needs — the HTTP server, the JSON parsing, the exposition format — comes
from the standard library.

**PowerShell gathers, Python shapes.** Python has no portable way to ask Windows for a
service state, the Defender status or the patch history. PowerShell does, in one readable
line each. Every collector talks to an injected `CommandRunner` rather than to
`subprocess`, which is what lets the whole suite run on a Linux CI runner while the real
calls are still exercised in the Windows job.

The property the design is really about: **one broken collector must not cost the whole
scrape**. If Defender is unreachable, disk and service metrics still arrive and the
failure shows up as `windows_health_collector_up{collector="defender"} 0`, not as a gap in
a dashboard that nobody can explain a week later.

## Metrics

| Metric | Meaning |
|---|---|
| `windows_health_up` | The exporter answered the scrape. |
| `windows_health_collector_up{collector}` | That collector gathered its metrics without failing. |
| `windows_health_scrape_duration_seconds` | How long the last scrape took. |
| `windows_health_info{hostname,os_caption,os_version,exporter_version}` | Identity, as labels on a constant 1. |
| `windows_health_boot_time_seconds` | Unix timestamp of the last boot. |
| `windows_health_pending_reboot` | 1 while servicing is waiting for a restart. |
| `windows_health_disk_total_bytes{drive}` / `..._free_bytes{drive}` | Size and free space per fixed disk. |
| `windows_health_service_installed{service}` | The watched service exists on this client. |
| `windows_health_service_up{service}` | The watched service is running. |
| `windows_health_last_update_install_time_seconds` | Unix timestamp of the last installed update. |
| `windows_health_defender_realtime_protection` | Real-time protection is on. |
| `windows_health_defender_service_enabled` | The Defender service is enabled. |
| `windows_health_defender_signature_age_days` | Signature age, as Defender reports it. |

Installed and running are separate metrics because a service that was never deployed and
one that crashed are different problems for whoever is on call. A client whose patch date
cannot be determined exports no sample at all rather than a zero, which would claim it was
last patched in 1970.

## Setup

```bash
git clone https://github.com/ckuzu3/windows-health-exporter.git
cd windows-health-exporter
python -m venv .venv
.venv/Scripts/activate          # Linux and macOS: source .venv/bin/activate
pip install -e ".[dev]"

ruff check . && ruff format --check . && mypy && pytest --cov
```

Running it on a client:

```powershell
# Serve metrics for Prometheus to scrape
python -m windows_health_exporter --service wuauserv --service WinDefend

# Or print one scrape, for a scheduled task writing into the textfile collector
python -m windows_health_exporter --once > C:\ProgramData\prometheus\windows_health.prom
```

`--host` defaults to `127.0.0.1`, because binding everywhere by default would expose a
client's state to the network. A scraper on another machine needs `--host 0.0.0.0` and a
firewall rule you chose on purpose. The port defaults to 9183, next to the well-known
windows_exporter on 9182 rather than on top of it.

Prometheus side:

```yaml
scrape_configs:
  - job_name: windows-health
    static_configs:
      - targets: ["ws-0001.example.internal:9183"]
```

## Example

One real scrape from the machine this was developed on, hostname replaced:

```text
# HELP windows_health_collector_up 1 when a collector gathered its metrics without failing.
# TYPE windows_health_collector_up gauge
windows_health_collector_up{collector="system"} 1
windows_health_collector_up{collector="disk"} 1
windows_health_collector_up{collector="service"} 1
windows_health_collector_up{collector="update"} 1
windows_health_collector_up{collector="defender"} 1
# HELP windows_health_info Identity of the client and the exporter, as labels on a constant 1.
# TYPE windows_health_info gauge
windows_health_info{exporter_version="0.1.0",hostname="WS-DEMO",os_caption="Microsoft Windows 11 Home",os_version="10.0.26300"} 1
# HELP windows_health_pending_reboot 1 when the client is waiting for a restart to finish servicing.
# TYPE windows_health_pending_reboot gauge
windows_health_pending_reboot 1
# HELP windows_health_service_installed 1 when a watched service exists on the client.
# TYPE windows_health_service_installed gauge
windows_health_service_installed{service="wuauserv"} 1
windows_health_service_installed{service="NotInstalledService"} 0
# HELP windows_health_service_up 1 when a watched service is running.
# TYPE windows_health_service_up gauge
windows_health_service_up{service="wuauserv"} 0
windows_health_service_up{service="NotInstalledService"} 0
```

That output says three true things about this machine at the time: a restart is pending,
the update service exists but is stopped, and `NotInstalledService` is not there at all.
The last two look identical in `windows_health_service_up` and are told apart by
`windows_health_service_installed`, which is the reason both metrics exist.

Useful queries:

```promql
# Clients that stopped reporting
up{job="windows-health"} == 0

# Days since the last installed update
(time() - windows_health_last_update_install_time_seconds) / 86400

# Free disk as a share, per drive
windows_health_disk_free_bytes / windows_health_disk_total_bytes

# A watched service that exists and is not running
windows_health_service_installed == 1 and windows_health_service_up == 0
```

[prometheus/alerts.yml](prometheus/alerts.yml) turns those into alert rules. Every rule has
a `for` long enough that a reboot does not page anyone, and an annotation that says what
the receiver is expected to do.

## Architecture

```text
  cli.py            arguments, then either --once or serve
     |
  server.py         http.server: /metrics and /healthz
     |
  registry.py       runs every collector, catches failures, adds the scrape's own metrics
     |
  collectors.py     system | disk | service | update | defender
     |
  powershell.py     CommandRunner: PowerShellRunner (real) or FakeRunner (tests)
     |
  metrics.py        families and samples -> exposition text
```

`timestamps.py` sits beside the collectors and makes one decision once: a Windows
timestamp without a zone is local time, because that is what the update registry key
writes, and everything leaves as UTC.

## Tests

```bash
pytest --cov            # 99 tests, 99 percent coverage, floor enforced at 90
pytest -m windows       # the tests that really call PowerShell; skipped off Windows
```

The suite runs anywhere, because the collectors take an injected runner. The tests that
need a real PowerShell carry a `windows` marker and are skipped elsewhere rather than
pretending to have passed.

CI runs lint, format check, mypy and the suite on Python 3.11, 3.12 and 3.13 on Linux, and
then runs it again on Windows, where those marked tests are not skipped and the agent is
started end to end against the runner itself.

## What I learned

- **Running it found what the tests did not.** The service collector failed whenever one
  of the watched services did not exist, because `Get-Service -Name` writes an error per
  missing name and PowerShell then exits non-zero — breaking exactly the case the
  `installed` metric was built to describe. My fake runner could not have caught it: the
  failure lived in the boundary the fake replaces. The query now enumerates and filters,
  which has no error path, and there is a test that asserts the query never uses the
  failing form again.
- **A fake is only as good as the boundary it stands in for.** That is the argument for
  keeping a small number of tests that really cross it, and for running the thing by hand
  before calling it done.
- **Deciding what "down" means is most of the work.** Stopped and never installed, unknown
  patch date and old patch date, Defender off and Defender unreachable: each pair reads the
  same on a dashboard unless the metrics are designed to tell them apart.
- **An exporter's own metrics are not boilerplate.** `collector_up` is what keeps a
  partial failure from looking like good news, and it is the first thing I would check
  before trusting anything else in the scrape.
- **No dependencies is a feature when deployment is the hard part.** The exposition format
  took a page of code and a handful of tests. On a fleet, that is cheaper than a package to
  install, pin and update on every client.

## Possible extensions

- A Grafana dashboard. I left it out on purpose: a few hundred lines of dashboard JSON
  that were never imported into a running Grafana would look like a feature and be a
  liability. The queries above are what I would build it from.
- More collectors: BitLocker protection status, certificate expiry, the local firewall
  profile. Each is one script, one class and one test file.
- A `--textfile-directory` option that writes atomically, so a scheduled task cannot leave
  a half-written file for the node exporter to read.
- Packaging as a Windows service, which today means a scheduled task at startup.
- An MSI or a signed executable, if this ever had to be deployed as software rather than
  copied.

## Known limits

- **The alert rules are unverified.** There is no Prometheus on the machine this was
  written on, so `promtool check rules` has not run against
  [prometheus/alerts.yml](prometheus/alerts.yml). The thresholds are judgement, not
  measurement.
- **The scrape is unauthenticated.** On localhost that is fine. Exposed to a network it
  needs at least a firewall rule, and really a reverse proxy with TLS.
- **Run by hand on Windows 11 only.** CI additionally runs the PowerShell tests and one
  end-to-end scrape on a Windows Server runner, so the scripts are known to work there
  too; I have not used it on a server myself.
- **A scrape takes a few seconds**, because each collector starts a PowerShell process.
  That is acceptable at a 60-second scrape interval and would not be at five.

## License

MIT, see [LICENSE](LICENSE).
