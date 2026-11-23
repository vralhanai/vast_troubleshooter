# Backlog

Everything the POC skipped, left unverified, or deliberately kept simple, plus
what we want to improve once we have access to **a real Vast host**, **an Ubuntu
machine or VM**, or **a Vast API key**.

Sources: the open items in [STEPS.md](STEPS.md), the notes in each step's
verification paragraph, [docs/rental-detection.md](docs/rental-detection.md), and
the design review. Last updated 2026-11-23, after step 15.

How to read this file:

- **Unblocked by** says what you need before the item can be done.
  `Vast host` = a real machine listed on Vast. `Ubuntu` = any Ubuntu 22.04 or 24.04
  machine, ideally with an NVIDIA GPU. `API key` = a Vast API key. `Decision` =
  a choice only the owner can make. `Nothing` = can be done now.
- **P1** = affects safety or correctness. **P2** = affects coverage. **P3** = polish.

---

## 1. The most important gap

The tool has **never run against a real Vast host or a real Linux GPU machine.**
Every collector parses fixtures written from documentation, not captured output.
All of the logic is tested, but the assumptions about real output are not.

**First thing to do with any Ubuntu GPU machine:** run
`sudo .venv/bin/vast-troubleshooter diagnose --json` and
`sudo .venv/bin/vast-troubleshooter collect <name>` for each collector, save the
raw command output (sanitized), and replace the fixtures in `tests/fixtures/`.

---

## 2. Needs a real Vast host

| P | Item | Why it matters | Detail |
|---|---|---|---|
| P1 | **Rental detection** (step 13) | Today the state can never be `none`, so **every disruptive fix is blocked** (deliberate). The tool cannot restart the daemon until this is solved. | The VM source is a stub. The container source treats any container as a rental (over-blocks). Needs: how a container rental and a VM rental really look, whether the host's own containers can be told apart, and whether the daemon exposes rental state anywhere. See `docs/rental-detection.md` for the exact samples to capture. |
| P1 | **`kaalia.log` real format** | The error counts and the network/Docker/GPU categories are keyword guesses. | Capture a log tail with no rental, a container rental, and a failure. Replace the generic patterns with real ones. Consider using the log as a third rental-evidence source. |
| P1 | **Real `systemctl show vastai.service`** | Restart-loop and not-installed detection are written from the generic systemd format. | Capture active, failed, and restart-loop states. |
| P2 | **Read the host setup guide** | https://cloud.vast.ai/host/setup/ was never read. It may list OS versions, a driver minimum, Docker version, port ranges, and log paths. | Re-check every threshold in `rules/*.toml` against it. |
| P2 | **Read the other unread Vast pages** | Kernel upgrade, SSH password login, Secure Boot, and the driver-upgrade details beyond the table. | Needed for the Secure Boot, SSH, and kernel rules (section 5). |
| P2 | **Which checks Vast really runs** | The verification page lists no individual checks and no numeric de-verification thresholds. | Compare our findings with what the Vast console shows for a real de-verified machine, and tune severities. |
| P2 | **Validate the two fixes for real** | Neither `restart-vast-daemon` nor `fix-dns-resolver` has run a real command. | Run both on a **disposable** VM first (section 3), then, if ever allowed, on a host with no rentals. |
| P3 | **Controller reset** | Vast's offline page step 3 (delete controller state files, restart, run `send_mach_info.py`) is the real fix for "service up, GPUs seen, still Offline". Left out because it is disruptive and the docs do not say it is safe with rentals. | Keep as a documented operator-led runbook, or add as a fix only with confirmed safety rules from Vast. |

---

## 3. Needs an Ubuntu machine (any, not necessarily on Vast)

### 3a. Replace written-from-documentation fixtures with real captures

| P | Fixture | What to capture |
|---|---|---|
| P1 | `gpu/`, `driver` | `nvidia-smi --query-gpu=...` and plain `nvidia-smi` on 1 GPU and on several. Also a broken driver. **Confirm which throttle field works: `clocks_event_reasons` or `clocks_throttle_reasons`.** |
| P1 | `kernel_log/` | `journalctl -k -b -g ...` and `dmesg -T`. **Confirm whether `journalctl -g` exits non-zero when nothing matches.** Real `NVRM: Xid` and AER wording on current drivers. |
| P1 | `docker/` | `docker info --format '{{json .}}'` with and without the NVIDIA runtime, daemon down, and permission denied. (Real Docker 29.9.3 on Windows already showed `ServerErrors: null` with the message on stderr only.) |
| P2 | `disk/` | `lsblk -P`, `df -P -B1`, `findmnt`, `smartctl -x` for NVMe (healthy and with errors), SATA SSD, and HDD. |
| P2 | `network/` | A real `/etc/resolv.conf` for systemd-resolved, static, and NetworkManager hosts. |
| P2 | `system/` | `/etc/os-release`, `uname -r`, `/proc/uptime` on 22.04 and 24.04. |
| P2 | `vast_host/` | See section 2. |

### 3b. Linux-only code that has never run

- Process-group kill on timeout, the safe `PATH`, and the real `sudo -n --` path (runner).
- `flock` lock, owner-only (0600) log and lock files, refusal to write through a symlink (fix engine). One POSIX-only test is skipped on Windows.
- `ip route`, `resolvectl dns / revert / flush-caches`, and `systemctl is-active` behavior.
- `docker ps -a --format {{.State}}` through the pinned socket.
- A manual run of `diagnose` as a normal user, with `sudo`, and with `--allow-root-reads`.

### 3c. Decisions this unlocks

| P | Item | Unblocked by |
|---|---|---|
| P1 | **Support matrix** (open question 9): which Ubuntu versions and driver branches the POC supports. Suggested: 22.04 and 24.04 LTS. | Decision |
| P1 | **Python 3.15 on Ubuntu.** Ubuntu ships an older default Python. Decide how it is installed (a `uv`-managed interpreter?), who updates it, and whether to lower the pin. | Ubuntu + Decision |
| P2 | **Passwordless sudo.** `--allow-root-reads` and `--use-sudo` use `sudo -n`, which fails if sudo needs a password. Decide: document it, or run the whole tool as root, or ship a narrow system-owned helper (the design review's preferred option). | Decision |

---

## 4. Needs a Vast API key (and a decision to leave "offline" mode)

The project decision is **offline: no Vast API calls.** The items below are only
possible if that changes. Nothing here is committed to. If it is ever approved:
keep the key in an environment variable or a 0600 file, never print or log it,
and keep a fully offline mode.

| P | Idea | What it would give |
|---|---|---|
| P1 | **Verify a maintenance or unlisted state** before a disruptive fix. | Today a host-local check cannot stop Vast from assigning a new rental after the check, and a scheduled maintenance window does not unlist the host. This is the main missing piece for safely fixing a **live** host. Needs `vastai unlist machine` / `schedule maint` style state, read and confirmed. |
| P1 | **Read rental state from Vast** (`vastai show machines`, instances). | A second, authoritative evidence source for the rental check, replacing guesswork. |
| P2 | **Read the verification state and the red error shown in the Machines tab.** | Show Vast's own reason next to our findings. Close the loop: "your fix cleared the error". |
| P2 | **Reliability score** and uptime history. | Explain "unverified because reliability < 90%", which cannot be seen locally today. |
| P2 | **Run or read the self-test** (`vastai self-test machine`). | It checks PCIe bandwidth, network speed, ports, and VRAM with Vast's own thresholds, which are not published. |
| P3 | **Machine id in reports**, opt-in and explicit, for support requests. | Today reports carry no machine identifier. |

---

## 5. Checks designed but not built

These were in the Level 1 design or the verified requirements, and no step built them.

### Collectors that do not exist

| P | Collector | Checks (from the Vast verification page) |
|---|---|---|
| P1 | `security` | **Secure Boot disabled** (`mokutil --sb-state`). **SSH password login disabled**, judged from the effective `sshd -T` policy including `Match` blocks, not the config file. Kernel patch status against Canonical's advisory data (a generic `uname` comparison misses backports). The Secure Boot rule in step 11 is waiting on this. |
| P1 | `cpu` | x86_64 or ARM64, AVX, **2 physical cores per GPU**. Also gives the architecture fact the ARM64 CUDA rule needs. |
| P1 | `memory` | **System RAM ≥ 0.95 × total VRAM**; ECC errors. |
| P2 | `pcie` | **Measured** PCIe bandwidth > 2.85 GiB/s per GPU. Today only the negotiated link gen/width are read (and not judged). AER detail beyond counts. |
| P2 | `bandwidth` | 500 Mbps down and up. Needs an approved speed-test method (third-party servers were approved in principle). Must stay opt-in and off by default. |
| P2 | Port reachability | **5 forwarded ports per GPU** (3 absolute minimum). A host cannot see its own WAN reachability: needs an external observation against an operator-authorized target. Local listening sockets are not enough. |
| P3 | Public IPv4 / CGNAT / wired Ethernet check | Vast requires a public IPv4 and no CGNAT. The `curl ifconfig.me` check was deferred because it sends the host's public IP to a third party. |

### Facts we collect but no rule uses yet

The data is there; the rules are not. Cheap wins once the thresholds are confirmed.

| Fact | Possible rule |
|---|---|
| `gpu.vram_min_mib` | VRAM per GPU > 7 GB |
| `gpu.models_identical` | All GPU models identical |
| `disk.hdd_count` | Docker storage must be an SSD |
| `disk.docker_shares_root_fs` | Docker needs a dedicated drive |
| `driver.version` + a GPU compute-capability fact | **NVIDIA driver below Vast's table** (595.91.07 / 610.57.04 / 580.178.04 by generation and branch). Needs `compute_cap` added to the GPU query and an `unknown` result for unlisted branches such as 620. The rule table in PLAN.md lists this, but step 11 did not build it. |
| `system.os_id`, `os_version_id` | Ubuntu Server 22.04 minimum; desktop editions unsupported |
| `kernel_log.aer_*_count`, `io_error_count` | PCIe AER errors; storage I/O errors |
| `vast_host.restart_loop`, `log_error_*` | Daemon restart loop; recurring daemon errors |
| `gpu.<i>.throttle_reasons`, temperature | Thermal or power throttling |
| `gpu.<i>.pcie_gen/width` | PCIe link narrower than expected |
| `gpu.<i>.ecc_mode` | ECC state |

### Rules that cannot be expressed yet

- The rule language has **no `not`, no aggregation, and no per-item wildcard.** Aggregate facts (`smart_failed_count`, `sata_pending_total`, ...) are a workaround, so a finding cannot say *which* drive or GPU is bad. Consider per-device findings.
- ARM64 CUDA minimum (12.6) needs an architecture fact.

---

## 6. Deliberately skipped (still worth doing, with care)

| Item | Why it was skipped | What it needs |
|---|---|---|
| Docker GPU test run (`docker run --gpus all ...`) | Pulls an image and uses the GPU, so it can compete with rentals. Registering the NVIDIA runtime does not prove a GPU container can start. | Opt-in only: an image pinned by digest, explicit pull consent, a verified idle maintenance state, bounded resources, cleanup, no customer mounts. |
| SMART self-tests (`smartctl -t`) | They write and compete with client I/O. A test enforces that none can be added to the read allowlist. | Only on a verified idle host. |
| `ping`, HTTP checks, `curl ifconfig.me` | Leak the host's address to a third party; ping needs a raw socket or a subprocess. | An explicit user choice per check. |
| Speed test | Third-party servers; load on the link. | Opt-in, destinations listed first. |
| RAID controller SMART | Needs controller-specific `smartctl -d` types. | A host with RAID to test on. |
| VM rentals | Backend unknown. | Section 2. |
| Parallel collection | Design flow A says collectors run in parallel. They run one after another, and `docker info` is run by three collectors. | Nothing. Add a per-run result cache and optional parallelism. |
| ARM64 | Needs different CUDA minimum and a cpu collector. | Section 5. |
| Non-NVIDIA GPUs | Vast requires NVIDIA. | Out of scope. |

---

## 7. Reports and the rest of the POC plan (steps 16 to 19)

Not done yet, in `STEPS.md`:

- **Step 16:** shareable JSON and Markdown report with redaction. The design review asks for: an allowlisted schema as the main protection (redaction is a second layer), **no `--no-redact`**, restrictive file permissions, symlink and collision handling, a preview of exact files before sharing, no uploads, a local-only default, retention rules, and tests that seeded secrets never appear in any output including error paths. Open: how a support routing identifier is added (explicitly, opt-in).
- **Step 17:** Docker image for Windows dev and test. Non-root, no Docker socket, no host mounts. GPU passthrough only for the supported WSL 2 setup, labelled as container observations.
- **Step 18:** POC end-to-end test on a disposable Ubuntu VM with a real injected fault. **Needs a VM.**
- **Step 19:** packaging (`uv build`), checksums, frozen install, a pinned container base image, a changelog, and a documented path for security fixes. The rule TOML files already ship in the wheel (checked once).

---

## 8. Safety and design improvements

| P | Item | Notes |
|---|---|---|
| P1 | **Prevent a new assignment during a repair.** | Unsolved without the API. The lock only serializes this tool's own runs. Until solved, disruptive fixes should stay advisory on production hosts. |
| P1 | **Narrow privileged helper** instead of `sudo -n` on the tool's own commands. | The design review prefers a small system-owned launcher with fixed operations, kept out of user-writable directories. Today privilege is `sudo -n -- <fixed argv>`. |
| P2 | **Per-fix real postconditions and stability windows.** | The restart waits 10 seconds once. Consider watching `NRestarts` and the service for longer. |
| P2 | **Action log retention and rotation**, and an `actions` command to read it. | The log grows forever. Decide retention. |
| P2 | **Run-to-run history** (drift, "did this get worse"). | Not designed. |
| P2 | **Per-boot and time-window evidence** for kernel log findings, plus recurrence rates. | Today it is the current boot only. |
| P3 | **Distinguish "problem found" from "cause confirmed".** | A DNS failure does not identify the broken component; keep recommendations as hypotheses. |
| P3 | **Document the JSON schema** (units, timestamps, versioning) and the exit codes in the README. | The code has `schema_version = 1` but no spec. |

---

## 9. Engineering hygiene (no external access needed)

| P | Item |
|---|---|
| P1 | **CI:** GitHub Actions running `ruff` and `pytest` on Ubuntu and Windows. None exists. Linux CI would also exercise the POSIX-only paths in 3b that we cannot run locally. |
| P2 | **Type checking.** No `mypy`/`pyright` is configured. The code carries a number of `# type: ignore` comments that have never been checked. |
| P2 | **README is out of date.** It still says "Design phase. No working code yet." Add install, usage (`diagnose`, `collect`, `fix`), exit codes, and the security model. |
| P2 | **PLAN.md is out of date.** The Level 3 milestone table is a summary of an older plan. STEPS.md is authoritative. Update or trim it. |
| P2 | **`.gitattributes`.** Every commit prints "LF will be replaced by CRLF" warnings on Windows. |
| P2 | **Untracked files:** `ReviewByCodex.md` (decide: commit as a record or ignore) and `.claude/` (local settings: add to `.gitignore`). |
| P2 | **Test helpers ship in the package** (`vast_troubleshooter/testing.py`). Move them under `tests/` or mark them as dev-only. |
| P2 | **Dependency and supply-chain checks:** a vulnerability scan, an update cadence, and pinning. |
| P3 | **Re-verify thresholds on a schedule.** Every rule records its Vast page and check date (all 2026-11-23). Add a reminder or a link checker. |
| P3 | **Coverage report** and a minimum. |
| P3 | **License holder:** `LICENSE` says "Copyright (c) 2026 vralhanai" (taken from the package author). Change it if you want a legal name. |

---

## 10. Quick start when you get access

**You get an Ubuntu machine (no Vast):**
1. Install, then run every `collect <name>` and `diagnose --json` as a normal user and with `sudo`. Save sanitized raw output.
2. Replace the fixtures (3a). Fix whatever breaks.
3. Run the Linux-only checks (3b) and add Linux CI (section 9).
4. Do step 18 on a disposable VM.

**You get a Vast host:**
1. Capture the rental samples listed in `docs/rental-detection.md` under three conditions.
2. Capture `kaalia.log` and `systemctl show vastai.service`.
3. Implement the VM source and narrow the container source. Only then can `none` be produced.
4. Read the host setup guide and re-check all thresholds.

**You get an API key (and approve using it):**
1. Decide the offline-mode boundary and key handling first.
2. Add a maintenance-state read (section 4, first row). That is what makes fixing a live host defensible.
