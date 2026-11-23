# POC end-to-end results

Run on 2026-11-23 by `bash e2e/run_poc.sh`. The full, unedited output of the last
run is in [poc-transcript.txt](poc-transcript.txt). Every excerpt below is copied
from it. Step numbers (`### 4.` and so on) refer to that file.

## Verdict

**The POC loop works on a real systemd machine**, with these limits, which matter:

- The test machine is a **container**, not a VM and not a Vast host.
- Through the real CLI the DNS fix **cannot be applied**. That is the designed,
  fail-closed behavior (the VM rental source is a stub), and the run confirms it.
- The apply, the success, and the rollback ran through a **test harness** that
  injects "no VM rentals". The harness is not part of the tool.

## What was real and what was a stand-in

| Real | Stand-in |
|---|---|
| Ubuntu 24.04.5, **systemd 255 as PID 1**, running as **root** | A container, not a VM (shares the Docker Desktop WSL2 kernel `6.18.58.1-microsoft-standard-WSL2`, arm64) |
| `systemd-resolved`, `resolvectl`, `systemctl`, `ip route` | `vastai.service` is `sleep infinity`, not the Vast daemon |
| The real `vast-troubleshooter` CLI and fix engine, real mutation commands, real rules | `docker` is a 10-line shell script (`e2e/docker`) answering only `ps -a --format {{.State}}`. There is no Docker daemon. |
| File modes, lock, action log, report writing | The VM rental evidence is **injected** in harness steps 8, 9, 12, 13, 14 (`e2e/driver.py`). The CLI steps use the real VM source, which says `unknown`. |
| Internet DNS via Docker Desktop's NAT | The faults were injected by this script |

The container is started with `SYS_ADMIN` and `NET_ADMIN`, unconfined seccomp and
AppArmor, and a cgroup mount. It is **not** `--privileged`. The test machine image
is never the default build (a test checks this).

## Results against the plan

| Planned check | Result | Where |
|---|---|---|
| Inject a known fault: break DNS for `vast.ai` | Done. systemd-resolved with **no upstream DNS server**. `getaddrinfo` fails: `RESOLUTION FAILED ... Temporary failure in name resolution`. | step 3 |
| Run `diagnose`. It must report the DNS finding | **Pass.** `CRITICAL DNS cannot resolve vast.ai (offline)`, evidence `network.dns_vast_ai_resolves = False [ok]`. | step 4 |
| Run the DNS fix dry-run, then apply with confirmation | **Dry run: pass.** **Apply: pass, through the harness only.** Through the real CLI: blocked, by design. | steps 6 and 9 |
| Re-run `diagnose`. The finding must clear | **Pass.** `0 problem(s)`, and `vast.ai` resolves again. | steps 9, 10 |
| Simulate an active rental using the step 13 evidence source, confirm the fix is blocked | **Pass.** One container "running" gives `rentals.state = active`. The fix is `BLOCKED ... rental state is active`. | step 7a |
| Remove that evidence: the state becomes `unknown` and the fix stays blocked | **Pass.** `rentals.state = unknown` (the VM source cannot prove absence), still `BLOCKED`. | step 7b |
| Generate a report. Check it is redacted | **Pass.** See "Report" below. | step 15 |

Also run, beyond the plan: the demo-only gate, the typed-confirmation path, a
forced real failure with automatic rollback, a refusal for other resolver setups,
the second fix (`restart-vast-daemon`) end to end, and the file permissions.

## The key outputs

**The fault is found (step 4):**

```
CRITICAL DNS cannot resolve vast.ai (offline)
  evidence: network.dns_vast_ai_resolves = False [ok]
```

**The real CLI is blocked, as designed (step 6):**

```
$ vast-troubleshooter fix fix-dns-resolver --demo-disposable-host
BLOCKED fix-dns-resolver
  rental state is unknown: VM rental detection is not implemented (the Vast docs do not say how VM rentals appear on the host)
```

Piping the confirmation into `--apply` (`fix-dns-resolver`) gave the same result:
the run is blocked before any prompt. In production no disruptive fix can run
until VM detection is implemented from a real host (see
[rental-detection.md](rental-detection.md)).

**Dry run, with the exact commands (harness, step 9):**

```
DRY_RUN fix-dns-resolver
  would run (nothing has been changed):
    /usr/bin/resolvectl dns eth0 1.1.1.1 8.8.8.8
    /usr/bin/resolvectl flush-caches
  rental state: none
  check before: fail, after: -
```

**Apply, then the real system state (step 9):**

```
SUCCEEDED fix-dns-resolver
  the check now passes
  check before: fail, after: pass
$ resolvectl status eth0
Link 2 (eth0)
    Current Scopes: DNS
         Protocols: +DefaultRoute -LLMNR -mDNS -DNSOverTLS DNSSEC=no/unsupported
Current DNS Server: 1.1.1.1
       DNS Servers: 1.1.1.1 8.8.8.8
$ python3 -c "...getaddrinfo('vast.ai'...)"
vast.ai -> 76.76.21.21
```

**A real failure, and the automatic rollback (step 12).** With 1.1.1.1 and 8.8.8.8
blackholed (`ip route add blackhole ...`) the change cannot work:

```
FAILED fix-dns-resolver
  the problem is still present
  rolled back
  check before: fail, after: fail
  the change was rolled back
[exit 4]
$ resolvectl status eth0
Link 2 (eth0)
    Current Scopes: none
```

The link is back to its original state (no DNS servers of its own).

**Other resolver setups are left to the operator (step 13).** With a static
`/etc/resolv.conf`:

```
BLOCKED fix-dns-resolver
  /etc/resolv.conf is not the plain systemd-resolved stub (split DNS, a VPN, or a static file); change DNS by hand
```

**The restart fix on real systemd (step 14).** The unit was stopped, `diagnose`
reported `CRITICAL vastai.service is not running`:

```
DRY_RUN restart-vast-daemon
  would run (nothing has been changed):
    /usr/bin/systemctl restart vastai.service
SUCCEEDED restart-vast-daemon
  check before: fail, after: pass
$ systemctl is-active vastai.service
active
```

A second run said `nothing to fix: the check already passes`, so a running
service is never restarted. The 10-second settle time is visible in the action
log: `apply_started` at `23:28:31`, `apply_finished` at `23:28:41` in the first run.

## Report (step 15)

Written on real Linux as root: `-rw-------` for both files. Checks, run inside the
machine:

- Identifying text (`poc-machine`, `172.17.0.`, `192.168.65.`, `root@`): **0** matches.
- `raw_excerpt` or `source_command` in the JSON: **0** matches.
- Environment named in the report: `"environment": "container on a WSL2 kernel"`.
- Every IP address in either file other than the two well-known public resolvers:
  **none**. The resolvers (`1.1.1.1` 5 times, `8.8.8.8` 3 times) appear because the
  DNS advice and the fix commands name them. They are left readable on purpose.

## State and permissions (step 16)

```
drwx------ root root  /var/lib/vast-troubleshooter
-rw------- root root  actions.jsonl
-rw------- root root  fix.lock
```

The action log has one `run_id` per run and the phases `blocked`, `dry_run`,
`apply_started`, `command`, `apply_finished`. A search for `stdout`, `stderr`,
`password`, and `token` in it found **0** matches: command output is never stored.

## What went wrong along the way (reported, not hidden)

1. **My first DNS scenario was invalid.** The baseline was not healthy (the stub
   resolver had no upstream, so DNS failed from the start), and the injected
   "dead upstream" (`192.0.2.1`) did **not** break DNS: **Docker Desktop's network
   answers DNS sent to any address.** The tool behaved correctly both times (it
   found the baseline failure, and reported "no problem" when there was none), but
   the DNS fix was never exercised. The scenario was rewritten to use a fault that
   is real here: a resolver with no upstream. The transcript is from the corrected
   run. A first run is not kept.
2. **My first leak check was sloppy.** Its pattern included `1.1.1.1`, which
   legitimately appears in the report, so it counted 5 "leaks". It was split into
   an exact "identifying text" check and a separate listing of public-resolver
   mentions.
3. **A Dockerfile ordering problem.** Adding the `e2e` stage last would have made a
   plain `docker build .` produce the root-running test machine. A final stage now
   keeps the default build the hardened runtime, and a test enforces it.

## Not verified, and what this does not prove

- **No real Vast host.** The Vast daemon, the real `kaalia.log`, and how a real
  rental looks on a host are all still unknown.
- **No VM.** A container shares the Docker Desktop kernel. Real hosts have their
  own kernels, drives, GPUs, and PCIe.
- **No NVIDIA GPU, no GPU checks, no SMART, no kernel-log content** in this run.
- **`--use-sudo` was not exercised.** The test machine runs as root.
- **The interactive confirmation prompt** was exercised only in unit tests and in
  step 6 (which was blocked before the prompt). In the harness steps the harness
  supplies the typed id itself. A human typing it in a real terminal against a
  fix that can run has not been done, because the CLI cannot run one.
- **arm64 only.** An `amd64` run has not been done.
- **Docker is a stand-in.** The rental check's container evidence came from a
  shell script, not a Docker daemon.

## Reproduce

```powershell
docker build --target e2e -t vast-troubleshooter:e2e .
bash e2e/run_poc.sh          # writes docs/poc-transcript.txt and refreshes tests/fixtures/real_systemd_container/
```

Needs Docker Desktop and Git Bash. The test machine is removed when the script
ends. Run it only on a development machine.
