# Walkthrough

Why the exporter is built this way. The README says what it does; this says why, and where
it stops.

## 1. Two constraints that shaped everything

**It gets copied onto managed clients.** Not deployed into a cluster, not run in a
container — copied onto machines somebody else administers. That makes every dependency a
thing to install, pin and update on all of them, so there are none. The HTTP server is
`http.server`, the parsing is `json`, and the exposition format is a page of code in
`metrics.py`. `prometheus_client` would have been the obvious choice in any other setting;
here it would have been the single hardest part of the rollout.

**The facts only PowerShell can reach.** Service state, Defender status, patch history,
pending reboot: Python has no portable way to any of them. PowerShell has a readable line
for each, which a Windows administrator can check without reading Python. So PowerShell
gathers and Python shapes, and the boundary between them is one protocol.

## 2. The boundary: CommandRunner

```python
class CommandRunner(Protocol):
    def run_json(self, script: str) -> Any: ...
```

`PowerShellRunner` runs the real thing. `FakeRunner` answers from a prepared table. Every
collector takes one, so the entire suite runs on a Linux CI runner.

Three details in the real runner are not decoration:

- **`-NoProfile -NonInteractive`.** An agent runs unattended. A user profile that prints
  something would break the JSON, and a prompt would hang the scrape forever.
- **A timeout.** A scrape has a deadline. A wedged query must fail rather than block.
- **Empty output is `None`, not an error.** A query that legitimately finds nothing writes
  nothing at all, and that is an answer.

`as_list` deserves its own mention. PowerShell serialises one row as an object and several
as an array, so a collector that iterates over the result would walk the *keys* of a single
row instead of its fields. It is the most common way to get Windows JSON wrong, it is one
function, and it has its own test with a single disk.

## 3. The property the design is actually about

One broken collector must not cost the whole scrape.

```python
try:
    families.extend(collector.collect())
except Exception:
    logger.warning("Collector '%s' failed", collector.name, exc_info=True)
    up_samples.append(Sample(value=0.0, labels={"collector": collector.name}))
    continue
```

That `except Exception` is broad on purpose, and it is the one place in the project where
that is right. A collector reaches into Windows through a subprocess; the ways it can fail
are not enumerable from here. What matters is that the failure becomes
`windows_health_collector_up{collector="defender"} 0` while disk and service metrics still
arrive.

The counterpart is that **no collector catches its own errors**. A collector that swallowed
a failure would return a plausible metric that is quietly wrong, which is worse than one
that is visibly missing. There is a test asserting that `DefenderCollector` lets a
`PowerShellError` escape.

The exporter's own metrics follow from the same thought. `windows_health_up`,
`collector_up` and `scrape_duration_seconds` come first in the document, because they are
what a reader checks before trusting anything below them.

## 4. Deciding what "down" means

Most of the work in an exporter is not plumbing, it is deciding which distinctions are
worth a metric. Three pairs that read identically unless you separate them:

**Stopped versus never installed.** `windows_health_service_up` is 0 for both. A service
that crashed needs someone tonight; one that was never deployed on that model of laptop
needs nobody. `windows_health_service_installed` is the second metric that tells them
apart, and the alert rule uses both: `installed == 1 and up == 0`.

**Unknown patch date versus old patch date.** A client that cannot report when it was last
patched exports **no sample**. Exporting a zero would claim it was patched in 1970 and
would put it at the top of every "most overdue" list for the wrong reason. In Prometheus an
absent series means "no data", which is exactly the truth. The two cases then get two
rules: a threshold on the age, and `absent_over_time` for the silence.

**Defender off versus Defender unreachable.** Off is
`windows_health_defender_realtime_protection 0`. Unreachable is
`windows_health_collector_up{collector="defender"} 0` with no Defender metrics at all. A
single "defender_ok" gauge would have collapsed a security finding and a permissions
problem into one number.

## 5. The bug that only running it could find

The agent passed 98 tests and then failed the first time I pointed it at a real machine
with a service name that does not exist:

```
windows_health_collector_up{collector="service"} 0
PowerShellError: PowerShell exited with 1: no error output
```

`Get-Service -Name A,B,C` writes an error record for every name it cannot find. Even with
`$ErrorActionPreference = 'SilentlyContinue'`, PowerShell's process exit code ends up
non-zero, so the runner raised and the whole collector reported down — in precisely the
case the `installed` metric exists to describe.

The fix is not a louder `-ErrorAction`. The query now enumerates and filters:

```powershell
$wanted = @('wuauserv','WinDefend')
Get-Service | Where-Object { $wanted -contains $_.Name } | ...
```

No error path at all. Enumerating a few hundred services costs a moment, and a test now
asserts the query never uses `Get-Service -Name` again.

**What I take from it.** The fake runner could not have caught this, because the failure
lived exactly in the boundary the fake replaces. A fake is only as good as the thing it
stands in for, which is the argument for keeping a few tests that really cross the
boundary — and for running the program by hand before calling it finished. The test suite
is now stronger for the regression test, but the suite is not what found it.

## 6. Smaller decisions, and the reasoning

**Service names are validated, not escaped.** They are interpolated into a PowerShell
script. `SERVICE_NAME` is a conservative pattern, and a name outside it is refused at
construction time. A name with a semicolon in it is far more likely to be an injection
attempt or a mistake than a real service, so refusing is both safer and more honest than
quoting it and hoping.

**The default bind address is localhost.** Binding everywhere by default would publish a
client's patch level and Defender state to its network. Exposing it is a decision, so it
takes a flag.

**Port 9183.** windows_exporter owns 9182. Sitting next to it rather than on top of it
means both can run on the same client, which they often will.

**A 500 beats plausible zeros.** If the registry itself fails, `/metrics` answers 500.
Prometheus then records a failed scrape. Returning an empty but well-formed document would
have made every gauge read as missing-but-fine.

**Whole numbers render without `.0`.** Cosmetic, and `format_value` is a pure function with
a parametrised test, including Prometheus' own spelling of `NaN`, `+Inf` and `-Inf`, which
Python would otherwise write as `nan` and `inf`.

**`/healthz` does not scrape.** It answers from the process alone, so a liveness check
costs nothing and cannot be made to fail by a slow Windows query.

## 7. Timestamps

`timestamps.py` exists for one decision, not for a parsing trick. Two formats arrive:
`ToString('o')` with a zone, and the Windows Update registry key with local time and no
zone at all. `datetime.fromisoformat` reads both on the supported Python versions,
including the seven-digit fraction and the trailing `Z`.

What the module adds is the rule for the second format, made once instead of in three
collectors: **no zone means local time**, because that is what Windows wrote, and
everything leaves as UTC so an age never depends on where the client stands.

I had originally written a workaround for a `fromisoformat` limitation that does not exist
on Python 3.11 and newer. It is gone: a defensive branch nothing can reach is dead code,
and the comment explaining it was worse than useless because it was wrong.

## 8. Testing strategy

99 tests, 99 percent of statements, floor enforced at 90 through `fail_under` rather than a
number in a README. Warnings are errors.

**Layered.** The exposition format is tested as string comparisons. The runner is tested
with an injected `subprocess.run` — no process is spawned, so the argument shape, the
timeout, the non-zero exit and the unparsable output are all exercised in milliseconds. The
collectors are tested against prepared payloads. The server and the CLI are driven for
real, over HTTP, on a port the operating system picks.

**The marked tests.** A handful carry `@pytest.mark.windows`, run PowerShell for real and
are skipped by a `conftest.py` hook elsewhere. Skipped is the honest outcome: the suite
does not claim to have tested what it could not reach.

**Shutdown is tested.** `serve` was split out of `main` so a test can start it on a thread
and stop it again, and a second test asserts that Ctrl+C still closes the socket. A
half-closed socket keeps the port busy until someone reboots the machine, which is a bad
way to find out.

### What is not tested, and why

- **The alert rules.** No Prometheus on this machine, so `promtool check rules` has not
  run. The thresholds are judgement rather than measurement, and the README says so.
- **Windows Server.** The queries are standard, but I have run this on Windows 11 only.
- **The one line in `main` that starts a long-running server**, and the `__main__` module,
  which is covered by a subprocess test that coverage cannot see.
- **Concurrent scrapes.** The server threads, and two scrapes at once would start two sets
  of PowerShell processes. At a 60-second interval that does not happen; at a five-second
  interval it would, and the answer is a short-lived cache rather than a lock.

## 9. Tooling

Ruff with a wide rule set, including `S` (bandit), which is the right default for something
that spawns processes on managed clients. Two of its findings led to real changes rather
than suppressions: the `subprocess` import carries an explanatory `noqa` instead of a bare
one, and the places that had `Any` parameters now take `object` and narrow with
`isinstance`, which is both stricter and more honest. The only `Any` left is the return
type of `run_json`, because the shape of a JSON document genuinely is not known statically,
and it says so in a comment next to the suppression.

mypy strict over the sources and the tests. A test that type-checks still compiles after a
refactor.

## 10. If I had to extend this tomorrow

A new collector is a script constant, a class with `name` and `collect`, one line in
`build_collectors` and a test file. Nothing else changes, because the registry does not know
what its collectors do and the renderer does not know where the numbers came from.

The two things I would do before running this on a fleet: a firewall rule and a reverse
proxy if the scrape leaves localhost, and `promtool check rules` on the alerts so the
thresholds are at least syntactically real.
