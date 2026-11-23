# Vast Troubleshooter: Plan

This plan has three levels, as defined in the README. Each level builds on the one before it. Levels 2 and 3 are outlines and will be expanded next. Decisions from the README (section 8) are reflected here. Open items are listed in 1.15.

| Level | Document | Output | Status |
|---|---|---|---|
| 1 | Rough system design | Architecture, modules, data flow, security model | **Draft v2, decisions applied** |
| 2 | Detailed specification | Check specs, rule schema, CLI contract, test plan | Outline |
| 3 | Working system | Runnable tool, built milestone by milestone | Outline |

---

## POC scope

The POC is a **CLI proof of concept** that proves the core loop: collect, diagnose, fix, report. It runs on Ubuntu. Windows is used only as a Docker dev/test environment.

**Proposed POC scope (to be confirmed):**

- **In:**
  - CLI with `diagnose`, `fix`, and `report` commands.
  - Collectors: `system`, `gpu`, `driver`, `docker`, `kernel_log`, `network` (DNS and outbound only).
  - Rules for the offline and unverified cases in section 1.8: broken DNS, Xid/NVRM errors, driver/CUDA below minimum, Docker NVIDIA runtime missing.
  - Rental check (running containers) before any disruptive fix.
  - Guided fixes for a small set, each with dry run and undo notes. *Which fixes are in the first set is to be chosen.*
  - Redacted report in JSON and Markdown.
  - `uv` package. Docker image for dev/test on Windows.
  - MIT license.
- **Out (later milestones):**
  - Speed test (`bandwidth`) and full port checks.
  - Full `cpu`, `memory`, `disk`/SMART, and `pcie` collectors.
  - Vast host service deep checks.
  - Web UI.
  - Release hardening (signed artifacts, full support matrix).

**POC done when:** a diagnosis run on an Ubuntu host with a known fault (for example, a broken resolver) reports the right finding and, after confirmation, the guided fix resolves it. The rental check blocks the same fix while a simulated active rental is running, and it reports `unknown` (and blocks) when that evidence is removed.

---

## Level 1: Rough system design

### 1.1 Goal

Define how the tool is structured, which parts it contains, how they talk to each other, and which trade-offs are open. No code is written at this level.

### 1.2 Users and use cases

- **Host operator** (primary): runs the tool on an Ubuntu machine when the host is offline, unverified, or slow. Wants a clear answer and a next step.
- **Developer on Windows** (secondary, dev and test only): runs the Linux tool in a Docker container. Vast does not accept Windows hosts.
- **Support requester**: attaches the tool's report to a Vast support request.

Core use cases:

1. **Quick check:** "Why is my machine offline or unverified?" Run all checks, show prioritized findings.
2. **Targeted check:** "Is my GPU healthy?" Run one category.
3. **Report:** Produce a redacted report file (human-readable and JSON).
4. **Guided fix:** Show a fix for a finding, apply it only after confirmation, and show how to undo it.

### 1.3 System context

```
+-------------------+            +-----------------------------+
|  Host operator    | -- CLI --> |  vast-troubleshooter        |
+-------------------+            |  (Python, runs on host or   |
                                 |   inside Docker on Windows) |
                                 +--------------+--------------+
                                                |
             +-----------------+----------------+------------------+
             |                 |                |                  |
        Linux OS          NVIDIA stack     Docker engine      Vast host service
  (dmesg, /proc, sysfs,  (nvidia-smi,      (daemon, GPU       (systemd unit,
   smartctl, DNS, ports)  NVML, driver)     runtime, test run) local logs)
                                                |
                                  [opt-in outbound probes only]
                                                |
                                 Public endpoints (DNS, Vast
                                 domains, speed-test servers)
```

The tool reads local state. By default it talks to the network only for local DNS lookups. Outbound probes and speed tests run only after explicit opt-in. It never calls the Vast API.

### 1.4 Architecture overview

The tool has six parts. Each part has one job and a narrow interface.

```
 Collectors  -->  Normalized facts  -->  Rule engine  -->  Findings  -->  Reporter
 (read state)     (typed records)        (diagnose)       (ranked)       (CLI / JSON / MD)
                                              |
                                              v
                                        Fix engine (opt-in, dry-run first)
```

| # | Component | Responsibility | Inputs | Outputs |
|---|---|---|---|---|
| 1 | **Collectors** | Gather raw facts from the system. One collector per area. Read-only. | OS, NVIDIA tools, Docker, files, logs | `Fact` records |
| 2 | **Normalizer** | Parse raw output into typed records. Tolerates format differences across versions. | Raw text or JSON | `Fact` models |
| 3 | **Rule engine** | Match facts against diagnosis rules. Produces findings with severity, evidence, and cause. | `Fact` models, rule files | `Finding` models |
| 4 | **Fix engine** | Run named, whitelisted fixes. Supports dry-run and undo notes. Enforces rental safety. | `Finding`, user confirmation | Action log, result |
| 5 | **Reporter** | Format results. Apply redaction. Write files. | `Finding`, `Fact`, action log | Terminal output, JSON, Markdown |
| 6 | **Rule data and fix registry** | Rule files hold conditions, thresholds, and text only. Fix commands, risk classes, rental gates, and privilege rules live in reviewed code, never in rule files. | Files shipped with the tool; code | Loaded at startup |

### 1.5 Collector areas

Each collector is independent, so one failing collector never stops the others.

| Collector | What it reads | Typical sources |
|---|---|---|
| `system` | OS version, kernel version, uptime, load | `/etc/os-release`, `uname`, `/proc` |
| `cpu` | Model, cores, load, throttling | `/proc/cpuinfo`, `psutil`, `/sys` |
| `memory` | Total, free, swap, ECC errors | `/proc/meminfo`, `psutil`, `edac` in sysfs |
| `disk` | Root free space, Docker storage size, SSD vs HDD, SMART health | `psutil`, `lsblk -d -o NAME,ROTA,SIZE,MODEL,TRAN`, `df`, `docker info`, `smartctl -H` and `smartctl -x` / `nvme smart-log` (root) |
| `gpu` | GPU count, model, temp, clocks, power, ECC, state | `nvidia-smi --query-gpu`, NVML |
| `driver` | Driver version, CUDA version | `nvidia-smi`, `/proc/driver/nvidia` |
| `pcie` | Link width and speed, AER errors | `lspci -vv`, sysfs, kernel log |
| `kernel_log` | NVRM / Xid messages, PCIe errors, I/O errors | `dmesg` or `journalctl -k` (needs root) |
| `docker` | Daemon up, version, NVIDIA runtime present, a short GPU test run | `docker info`, `docker run --gpus` |
| `network` | DNS resolution of Vast domains (local, default). Outbound reachability and speed tests are opt-in, with destinations listed first. Open ports are out of POC scope. | `getaddrinfo`; outbound probes only with explicit opt-in |
| `bandwidth` | Download/upload throughput | Speed test to third-party servers (approved). Out of POC scope. |
| `vast_host` | Vast daemon status and log | `systemctl status vastai.service` (unit `vastai.service`, "Vast.ai Host Daemon"); `/var/lib/vastai_kaalia/kaalia.log` |
| `security` | Kernel version against known-vulnerable list, driver version against minimum | Rule data plus `uname` and `nvidia-smi` |

Note: checked against the Vast docs in Level 2. See "Verified requirements" below.

### 1.6 Data model (conceptual)

- **Fact**: `{ id, collector, key, value, unit, timestamp, source_command, raw_excerpt }`.
- **Rule**: `{ id, title, applies_when (conditions on facts), severity, cause, evidence_keys, recommendation, fix_id (optional) }`.
- **Finding**: `{ rule_id, severity (critical/warning/info), category (offline, unverified, performance, info), summary, evidence (facts), recommendation, fix_available }`.
- **FixAction**: `{ id, description, requires_root, requires_no_active_rental, dry_run_output, undo_notes }`.
- **Report**: `{ host_summary, generated_at, findings[], facts[], actions[], redaction_applied }`.

Severity uses three levels. Category tells the operator which Vast status the problem affects. Check status is `pass`, `fail`, `unknown`, or `skipped`. A missing or unknown critical input makes the diagnosis incomplete, even when no rule fired. Unknown is never reported as pass. The JSON schema is versioned, and rule source and date are recorded. The tool estimates local conditions only. It does not determine marketplace status.

### 1.7 Key flows

**Flow A: Full diagnosis**

1. CLI starts and loads the rule and fix catalogue.
2. Collectors run in parallel with per-command timeouts.
3. Normalizer converts output into `Fact` records. Failed commands become facts with an error status, not crashes.
4. Rule engine evaluates all rules and produces findings.
5. Reporter prints findings, most severe first, with evidence and recommendations.

**Flow B: Guided fix**

1. Operator selects a finding with `fix_available`.
2. Fix engine checks preconditions: root access, active-rental check, fix allowed by catalogue.
3. Dry run shows exactly what would change.
4. Operator confirms in the terminal. The default answer is "no".
5. Fix runs. The action is logged with its undo notes.
6. Collectors re-run for the affected area to confirm the fix worked.

**Flow C: Report export**

1. Reporter collects findings, facts, and actions.
2. Redaction removes secrets, IPs, hostnames, and machine IDs.
3. Output is written locally as JSON and Markdown.

### 1.8 Rule examples (illustrative)

| Symptom in facts | Likely cause | Category | Recommendation |
|---|---|---|---|
| DNS lookup for Vast domains fails, network up | Broken DNS resolver | Offline | Check `/etc/resolv.conf` and resolver service |
| Kernel log shows NVRM Xid errors | GPU or driver fault | Offline / unverified | Collect Xid code, check driver version, check PSU and riser |
| `nvidia-smi` fails, driver loaded | Driver and library mismatch | Unverified | Reinstall driver matching the minimum version |
| Driver below Vast table minimum for its GPU generation | Outdated driver | Unverified (if confirmed) | Advisory. Show the installed version and the table minimum. Unlisted branches show as unknown. |
| CUDA version below 11.8 (12.6 on ARM64) | Outdated CUDA stack | Unverified | Update driver or CUDA |
| Forwarded ports below 5 per GPU (self-test fails below 3 direct ports) | Firewall or router config | Unverified | List closed ports, check firewall rules and router forwarding |
| PCIe bandwidth below 2.85 GiB/s per GPU | Bad slot, riser, or BIOS setting | Performance / unverified | Check slot and BIOS settings |
| NVMe critical warning not 0x00, or media errors above 0 | Failing disk | Unverified | Back up and replace disk (per SSD Health page) |
| SATA reallocated or pending sectors above 0 | Failing disk | Unverified | Back up and replace disk |
| System RAM below 0.95 x total VRAM | Insufficient RAM | Unverified | Add RAM (formula from verification page) |
| Secure Boot enabled, or SSH password login enabled | Host configuration | Unverified | Disable Secure Boot; use SSH keys only |
| Docker GPU test run fails | NVIDIA container runtime not configured | Unverified | Reconfigure runtime |

Thresholds are read from rule data, not hard-coded. The values used here are from the Vast pages checked on 2026-11-23 (see "Verified requirements"). They must be re-checked before release.

### 1.9 Security model

- **Trust boundary:** The tool runs with the host's privileges. Anything it reads or runs is a potential risk, so the design uses allowlisted commands only.
- **Privilege tiers:**
  - *Tier 0 (no root):* system info, GPU info through `nvidia-smi`, DNS, port checks.
  - *Tier 1 (root for reading):* `dmesg`, `smartctl`, `lspci -vv`.
  - *Tier 2 (root for changing):* any fix. Always needs confirmation.
- **Rental safety:** Fixes that restart services, reboot, or stop containers check for active rentals first. If the check cannot be made, the fix is blocked.
  - **Detection method is not yet known.** The Vast docs do not say rentals run as Docker containers. They do show per-container states ("running", "exists (stopped)") and a "running (vm)" state, so a Docker-only check would miss VM rentals.
  - Candidate sources to inspect on a real host: the Vast daemon log (`/var/lib/vastai_kaalia/kaalia.log`), running Docker containers, and VM state.
  - Rule (from the design review): the state is `active`, `none`, or `unknown`. `none` needs fresh, complete evidence from every supported backend: VM, container, and allocated-but-stopped workloads. Missing, stale, unreadable, or conflicting evidence gives `unknown`.
  - `active` and `unknown` block disruptive fixes, including after the operator confirms.
  - The state is checked again after confirmation and immediately before any change.
  - A host-local lock only serializes this tool. It does not stop Vast from assigning a new workload. Disruptive fixes stay advisory on production hosts unless a maintenance state can be verified.
  - Blocked until a real Vast host (or a faithful test host) is available to inspect.
- **Data handling:** Local only by default. Shareable output is built from allowlisted typed fields only: no raw log lines, command lines, or excerpts. Redaction is a second layer. No customer data is read.
- **Docker on Windows:** Dev/test only. The container runs with the smallest set of mounts and devices needed. The design avoids `--privileged`. GPU access through Docker Desktop on Windows is limited and is verified in M8.
- **Privilege and target policy:** The tool runs as the invoking user. Elevated reads go through a fixed set of operations with approved argument schemas and a minimal environment, not through free-form `sudo`. Read collectors never change the system.
- **Docker access:** The default has no Docker socket mount and no remote daemon. `DOCKER_HOST`, Docker contexts, and other environment overrides are rejected when they point away from a trusted local endpoint. The Windows dev container runs non-root with no host socket, no host filesystem or log mounts, and no elevated capabilities.
- **Fix recovery:** Every fix states its target, expected change, failure behavior, and post-checks. Reversible file changes keep owner, mode, and prior content. A daemon restart cannot be undone by rollback: an interrupted workload cannot be restored. DNS changes are not a live-host POC fix. They are demo-only on a disposable VM and go through the resolver that owns the config.
- **Secrets:** No secrets in the code, the report, or the logs.
- **Full rules:** See [README.md section 6](README.md#6-security-guidelines).

### 1.10 Packaging and deployment (draft)

- **Ubuntu (primary):** Installed as a Python package with `uv` or `pip`. Runs directly on the host.
- **Windows (dev/test only):** A Docker image containing the same package, for running and testing the Linux tool on a Windows machine.
- Both use the same CLI and the same rule data. Interface is CLI only.

### 1.11 Error handling principles

- A failed collector creates a fact with an error, so the report shows what could not be checked.
- Timeouts are set per command. A hung command never blocks the run.
- The tool reports what it could not check rather than guessing that everything is fine.
- Every finding names the evidence it is based on.

### 1.12 Testing approach (draft)

- Unit tests for normalizers, using saved command output from real machines (sanitized).
- Rule tests: each rule has a fixture that should trigger it and one that should not.
- Fix tests: dry-run output tested for each fix. Real fixes only tested on disposable VMs.
- No test requires a real rental or touches a customer workload.

### 1.13 Design decisions and trade-offs

| Decision | Choice in this draft | Alternative | Why this choice |
|---|---|---|---|
| Rule storage | Data files (YAML/TOML) | Hard-coded Python | Rules change as Vast updates requirements |
| Collection | Read-only collectors, one per area | One big script | Isolation, easier tests, partial results |
| Fix model | Named, whitelisted fixes with dry-run | Free-form shell commands | Safety and auditability |
| Interface | CLI first | Web UI first | Simplest path that works on servers without a desktop |
| Language | Python | Go or Rust | Required by the project brief; good system libraries |

### 1.14 Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Vast requirements change | Wrong findings | Thresholds in data files, versioned, with a source link |
| Different NVIDIA driver and tool output across versions | Parsing breaks | Tolerant parsers, saved fixtures per version, explicit "unknown" result |
| Fix causes customer downtime | Lost rentals and trust | Rental check, default "no", dry run, no reboot without consent |
| Windows GPU access inside Docker is limited | Dev/test path does not give full GPU testing | Test GPU paths on a Linux machine; verify Windows limits in M8 |
| Tool is mistaken for an official Vast tool | Confusion | Clear project name and README disclaimer; no Vast branding |

### 1.15 Decisions and open items

Decided (see [README.md section 8](README.md#8-decisions-and-open-questions)): Windows is dev/test only, offline with no Vast API, diagnose and fix, rental detection on, CLI, shareable report, `uv` package, third-party speed tests allowed, MIT license.

Still open:
- Support matrix (Ubuntu and NVIDIA driver versions).
- Rental detection method (see 1.9). Blocked on a real host.
- First set of fixes for the POC.
- Driver: compare the installed driver with Vast's driver table (see Verified requirements). Advisory in the POC.

### 1.16 Sources (checked 2026-11-23)

- Vast.ai host docs (fetched): [verification stages](https://docs.vast.ai/host/verification-stages), [machine offline](https://docs.vast.ai/host/machine-offline), [how to self-test](https://docs.vast.ai/host/how-to-self-test), [SSD health](https://docs.vast.ai/host/ssd-health), [machine metrics](https://docs.vast.ai/host/machine-metrics), [hosting overview](https://docs.vast.ai/host/hosting-overview), [NVIDIA driver upgrade](https://docs.vast.ai/host/upgrade-nvidia-driver), [Understanding Verification](https://docs.vast.ai/host/understanding-verification).
- Not yet read: the host setup guide (https://cloud.vast.ai/host/setup/), the kernel upgrade page, and the SSH and Secure Boot pages. Some answers may be there.


The pages were fetched and read on 2026-11-23. Corrections from that read:
- The earlier search summary said "CUDA 12.0" and "3 open ports per GPU". The verification page says CUDA 11.8 and 5 forwarded ports per GPU (100 recommended). The self-test page says at least 3 direct open ports.
- Rentals are not stated to run as Docker containers (see 1.9).
- The service is `vastai.service`, and the log is `/var/lib/vastai_kaalia/kaalia.log`.

---

## Verified requirements (Vast docs, checked 2026-11-23)

These come from the Vast pages named in 1.16. Each row says where it came from. Anything not on a page is marked "not published". Rule data must use these values, and they must be re-checked before release.

**Verification minimums (verification-stages page)**

| Area | Requirement | Used by rule |
|---|---|---|
| GPU vendor and generation | NVIDIA, Maxwell or newer | `gpu` |
| VRAM per GPU | More than 7 GB | `gpu` |
| CUDA | 11.8 or newer. On ARM64: 12.6 or newer | `driver` |
| GPU models | All identical within one machine | `gpu` |
| PCIe bandwidth | More than 2.85 GiB/s per GPU | `pcie` (needs a measured value; see open items) |
| CPU | x86_64 or ARM64, AVX instruction set | `cpu` |
| CPU cores | 2 physical cores per GPU | `cpu` |
| System RAM | At least 0.95 x (VRAM per GPU x GPU count) | `memory` |
| Network | 500 Mbps down and 500 Mbps up; wired Ethernet; public IPv4 (no CGNAT) | `network`, `bandwidth` |
| Forwarded ports | 5 per GPU minimum; 100 per GPU recommended | `network` |
| OS | Ubuntu Server 22.04 LTS minimum; 24.04 LTS recommended. Desktop editions not supported | `system` |
| Kernel | Latest security patch level for the LTS release. No version number published | `system` (report only) |
| NVIDIA driver | Vast "Upgrade the NVIDIA Driver" table (checked 2026-11-23): Turing and newer (compute 7.5+) on 595.xx: minimum 595.91.07; Turing and newer on any other branch: minimum 610.57.04; Maxwell, Pascal, Volta (compute below 7.5): minimum 580.178.04. Branches newer than the table (e.g. 620) are "not directly supported yet". | `driver` (compare with table; advisory in POC) |
| SSH | Keys only; password login disabled | `security` |
| Secure Boot | Disabled | `security` |
| Storage | SSD. Dedicated Docker drive of at least 200 GB. At least 20 GB free on the root partition | `disk` |
| Reliability | Over 90%. New machines start low and rise with uptime | Vast-side, not checkable locally |
| Usage | Dedicated machine only. Mining, gaming, or desktop use fails verification | Not checkable locally |

**Self-test (how-to-self-test page)**

- Runs `vastai self-test machine <machine_id>` with the Vast CLI and an API key. This is a Vast-side tool, not part of this tool. The tool must not call it (offline decision).
- Checks driver and CUDA setup, network speed and stability, open ports, PCIe bandwidth, GPU VRAM and reliability, RAM and CPU, and a short simulated workload. No numeric thresholds for these are published on the page.
- Even with `--ignore-requirements`, the machine needs at least 3 direct open ports.
- The self-test needs no other jobs or instances running.

**Verification and de-verification (verification-stages page)**

- Automated. A machine is shown as Unverified in search while de-verified. The Machines tab shows a red error with the reason.
- Common causes listed: network instability, closed ports, low bandwidth; failing storage or insufficient PCIe bandwidth; `nvidia-smi`/NVML failures or container device-init errors; container launch failures or repeated runtime exceptions; abuse or policy violations.
- Most errors clear within 1 to 2 hours after the fix. Some need manual clearing by Vast staff.
- No numeric de-verification thresholds are published.

**Offline troubleshooting (machine-offline page), in order**

1. Connectivity, all three must pass: `ping -c 4 1.1.1.1`, `nslookup vast.ai`, `curl -4 ifconfig.me`.
2. Service and GPUs: `sudo systemctl status vastai` (unit `vastai.service`). `nvidia-smi` must list every GPU. Logs: `sudo tail -n 100 /var/lib/vastai_kaalia/kaalia.log`, `sudo dmesg -T | grep -iE 'nvrm|nvidia|xid|pcie|aer'`, `sudo journalctl -k -b | grep -iE 'nvrm|nvidia|xid|pcie|aer'`.
3. Controller reset (only if the service runs and all GPUs are seen, but the host stays Offline). Stops the service and deletes controller state files under `/var/lib/vastai_kaalia/`. **This is a disruptive fix. It must be behind the rental check, and the page does not say it is safe with active rentals.** The vendor procedure also resends machine information. Keep it as a link to the vendor runbook for operator-led maintenance, not a POC fix.
4. Contact Vast Support with machine ID, offline time, and logs (redacted).

**Warnings on the offline page:** do not reboot, stop containers, force-stop VMs, delete instance data, or change NVIDIA drivers while rentals are active, unless Vast Support says so.

**SSD health (ssd-health page)**

- Drives: `lsblk -d -o NAME,ROTA,SIZE,MODEL,TRAN` (ROTA 0 = SSD). Docker root: `docker info | grep "Docker Root Dir"`.
- NVMe: `sudo smartctl -H /dev/nvme0` must say PASSED. `sudo smartctl -x /dev/nvme0`: Critical Warning `0x00`; Available Spare above its threshold; Media and Data Integrity Errors `0`.
- SATA: `sudo smartctl -x /dev/sda`: attributes 5 (Reallocated), 197 (Pending), 187 (Reported Uncorrect) at 0. Attribute 199 (CRC) points to cabling.
- Kernel log: `sudo dmesg -T --level=err,warn | grep -iE "nvme|ata[0-9]|sd[a-z]|I/O error|EXT4-fs error"`. Root must be mounted `rw` (`findmnt -no OPTIONS /`).
- Packages: smartmontools 7.4 on Ubuntu 24.04. NVMe self-tests need 7.4; on 22.04 use `nvme-cli`.
- Read-only steps are safe with instances running. Self-tests compete with client I/O.

**Not published by Vast (must be decided by us, not invented):** kernel version minimum, numeric speed or PCIe threshold in the self-test, de-verification thresholds, and the rental-detection method.

**Pages not yet read:** host setup guide (https://cloud.vast.ai/host/setup/), kernel upgrade, SSH and Secure Boot pages. The driver upgrade page and Understanding Verification were read on 2026-11-23. Understanding Verification does not split newly evaluated from previously verified machines.

---

## Level 2: Detailed specification (in progress)

**Goal:** Turn the Level 1 design into a precise spec that an engineer can build from without guessing. The verified requirements above are the first part of it.

**Planned contents:**

1. Verified list of Vast host requirements and thresholds, with links to each doc page and the date checked.
2. Check catalogue: for each check, the exact command, parsing rule, threshold, severity, and fixture file.
3. Rule file schema (YAML or TOML) with examples and validation rules.
4. Fix catalogue: for each fix, preconditions, dry-run output, steps, undo notes, and risk level.
5. CLI contract: commands, flags, exit codes, JSON output schema.
6. Report format: JSON schema and Markdown template.
7. Redaction rules: patterns, examples, and what is never redacted.
8. Docker spec: base image, required mounts and devices, run command for Windows.
9. Security spec: privilege list per check, threat model, and checklist for review.
10. Test plan: fixtures, coverage goals, and the manual test matrix (Ubuntu versions, driver versions, GPU models).
11. Acceptance criteria per milestone for Level 3.

**Entry criteria:** Level 1 reviewed and open questions answered.

**Exit criteria:** Spec reviewed. Each Level 3 milestone has testable acceptance criteria.

---

## Level 3: Working system (outline)

**Goal:** A running tool, built in milestones. Each milestone is usable on its own. [STEPS.md](STEPS.md) is the authoritative step list. The milestones below are a summary.

| Milestone | Scope | Acceptance criteria (summary) |
|---|---|---|
| **M0: Skeleton** | Project layout, CLI entry point, config loader, logging, test setup | `vast-troubleshooter --help` works. Tests run in CI or locally. |
| **M1: System and GPU collectors** | `system`, `cpu`, `memory`, `disk`, `gpu`, `driver` | Output matches fixtures on at least two Ubuntu versions. Missing tools produce a clear error fact. |
| **M2: Kernel and PCIe collectors** | `kernel_log`, `pcie` | Xid and AER lines are detected from sample logs. |
| **M3: Rule engine and first rules** | Rule loader, evaluator, the offline and driver rules from section 1.8 | Each rule has a passing and a failing fixture. Findings show evidence. |
| **M4: Network and ports** | `network`, `bandwidth` (if approved), port checks | DNS failure detected in a test environment. Port report lists closed ports. |
| **M5: Docker and Vast host service** | `docker`, `vast_host` | Detects missing NVIDIA runtime and stopped host service. |
| **M6: Reports** | JSON and Markdown export with redaction | Redaction tests pass. No secrets in sample output. |
| **M7: Guided fixes** | Fix engine, dry-run, confirmation, undo notes, rental check | Each fix has a dry-run test. Blocked when rental check fails. |
| **M8: Windows Docker path** | Dockerfile, run instructions, GPU access notes | Runs on Windows with Docker Desktop. Limits documented. |
| **M9: Release** | Packaging (`uv`), checksums, install guide, support matrix, MIT license in package | Install works from a clean Ubuntu machine. |

The POC covers M0 to M3 and M5 (`docker` only) and M7 (a minimal fix set), plus the report from M6 in its simplest form. Milestones are ordered so that each POC item is built in the order listed above.

**Entry criteria:** Level 2 spec approved.

**Working rule for every milestone:** Read-only behavior first, fixes last. A milestone is done when its tests pass and its acceptance criteria are met, not when the code is written.
