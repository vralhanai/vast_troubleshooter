# Vast Troubleshooter: Implementation Steps

This is the step-by-step plan to build the POC described in [PLAN.md](PLAN.md). **Nothing here is implemented yet.** Work starts only after the plan is confirmed.

## How we work

For every step:

1. **Implement** only the scope of that step.
2. **Verify:** run the step's tests and lint, and run the step's manual check. Report the actual output. If anything fails, fix it before moving on.
3. **Commit:** one commit per step, with a message that names the step. Commit messages end with:
   `Co-Authored-By: Claude Haiku 5.5 <noreply@anthropic.com>`
4. **Stop and report** after each commit. The next step starts only after the user says to continue.

Tooling used throughout:

- `uv` for environments, dependencies, and builds.
- `pytest` for tests. Tests use recorded command output (fixtures). Most tests never run real `nvidia-smi` or `docker`.
- `ruff` for linting and formatting.
- Each collector runs through one command runner. Tests inject a fake runner.

Rules that apply to every step:

- No shell strings. Commands are passed as argument lists.
- Collectors are read-only. Only the fix engine (steps 14 to 15) may change the system.
- Each step's scope is fixed. If a step turns up a new decision, stop and ask.

## Blockers to resolve before the steps that need them

| Needed for | Blocker | Who |
|---|---|---|
| Steps 5 to 8 (real output) | Sample output from a real Ubuntu host with NVIDIA GPUs, or confirmation that the fixtures we write are enough. | User |
| Step 13 (rental detection) | A host or test machine where we can inspect what a rental looks like (containers, VM, daemon log). Docs do not settle this. | User |
| Steps 14 to 15 (fixes) | Choice of the first fix set. | User |
| Step 17 (Windows Docker) | Docker Desktop on Windows, and a decision on GPU access in the container. | User |
| Step 18 (POC test) | An Ubuntu machine or VM for end-to-end testing. Disposable, not a live rental host. | User |

Steps 0 to 4 and most of 9 to 12 need no hardware. They can run on this Windows machine with fixtures.

---

## Step 0: Commit the design baseline

**Goal:** Put the existing design documents under version control. The repo has no commits yet.

**Scope:**
- Add `README.md`, `PLAN.md`, `STEPS.md`, `LICENSE`, `pyproject.toml`, `.gitignore`, `.python-version`, and the existing `src/` stub.

**Verify:**
- `git status` shows only the expected files.
- Links in `README.md` and `PLAN.md` point to existing anchors (check by hand).

**Commit:** `docs: add design plan, README, and MIT license`

---

## Step 1: Project tooling and CLI skeleton

**Goal:** A runnable CLI that does nothing useful yet, with the tooling working.

**Scope:**
- Confirm Python 3.15 is available to `uv` (`requires-python >= 3.15` in `pyproject.toml`). If it is not available, stop and ask.
- Add `typer` for the CLI. Add `pytest` and `ruff` as dev dependencies. Commit `uv.lock`.
- Create `src/vast_troubleshooter/cli.py` with a root command and a `version` subcommand.
- Point the script entry at `vast_troubleshooter.cli:app`.
- Add `tests/` with one smoke test.

**Verify:**
- `uv run pytest` passes.
- `uv run ruff check .` and `uv run ruff format --check .` pass.
- `uv run vast-troubleshooter --help` and `uv run vast-troubleshooter version` run.

**Commit:** `feat(cli): add project tooling and CLI skeleton`

---

## Step 2: Core data models

**Goal:** The shared types every other part uses.

**Scope:**
- `models.py` with pydantic models:
  - `Fact`: `collector`, `key`, `value`, `unit`, `status` (ok, missing, error, timeout), `source_command`, `raw_excerpt`, `timestamp`.
  - `Severity`: critical, warning, info.
  - `Category`: offline, unverified, performance, info.
  - `Finding`: `rule_id`, `severity`, `category`, `summary`, `evidence` (fact keys), `recommendation`, `fix_id` (optional).
- Redaction is not part of this step. It is in step 16.

**Verify:**
- Unit tests for validation: missing required fields fail, unknown status fails, JSON round-trip works.
- `uv run pytest` and `ruff` pass.

**Commit:** `feat(models): add Fact, Finding, and related data models`

---

## Step 3: Command runner with allowlist

**Goal:** One safe way to run system commands, used by every collector.

**Scope:**
- `runner.py`:
  - Takes an argument list, never a shell string.
  - Only commands on an allowlist can run. Each allowlist entry names the command, its arguments, and whether it needs root.
  - Per-command timeout. On timeout, returns a result with status `timeout`. Never hangs.
  - Missing binary returns status `missing`. Non-zero exit returns status `error` with stderr.
  - Captures stdout and stderr. Output is treated as data only.
- A fake runner for tests, which returns fixture output keyed by command.
- Root is never escalated by the runner itself. If a command needs root, the runner reports that and does not run it unless the user has allowed root reads (see step 12).

**Verify:**
- Tests for: allowlisted command runs, unknown command refused, timeout, missing binary, non-zero exit, no shell is used (passed to subprocess as list with `shell=False`).
- Tests pass on Windows and Linux.

**Commit:** `feat(runner): add allowlisted, timeout-bounded command runner`

---

## Step 4: Collector framework and `system` collector

**Goal:** The plug-in shape for collectors, proven on the simplest one.

**Scope:**
- `collectors/base.py`: a `Collector` interface with `name`, `collect(runner) -> list[Fact]`. A failing collector returns error facts. It never raises into the caller.
- Collector registry and a runner loop that runs all collectors and keeps going if one fails.
- `collectors/system.py`: OS name and version (`/etc/os-release`), kernel version (`uname -r`), uptime.
- Fixtures for Ubuntu 22.04 and 24.04.

**Verify:**
- Tests: `system` collector against both fixtures; one collector that raises does not stop the others.
- Manual: `uv run vast-troubleshooter collect system` prints the facts (on Linux, or with the fake runner on Windows).

**Commit:** `feat(collectors): add collector framework and system collector`

---

## Step 5: `gpu` and `driver` collectors

**Goal:** Read GPU and NVIDIA driver state through `nvidia-smi`.

**Scope:**
- `gpu`: GPU count, name, VRAM total, temperature, power, clocks, throttle reasons, ECC state, PCIe generation and width. Uses `nvidia-smi --query-gpu=... --format=csv,noheader,nounits`.
- `driver`: driver version and CUDA version from `nvidia-smi`.
- Handles the failure cases on the machine-offline page: "NVIDIA-SMI has failed", fewer GPUs than expected, `ERR!` or `Unknown Error` columns, and a hang (timeout).
- Fixtures: one healthy 1-GPU and 8-GPU output, one failed-driver output, one with `ERR!`.

**Verify:**
- Tests for each fixture, including the failure fixtures.
- Requires a real Ubuntu host sample (see blockers). Until then, fixtures are written from the `nvidia-smi` documentation and marked "unverified against real output".

**Commit:** `feat(collectors): add gpu and driver collectors`

---

## Step 6: `docker` collector

**Goal:** Know whether Docker runs and whether the NVIDIA runtime is set up.

**Scope:**
- `docker info` (daemon up, version, runtimes list, Docker Root Dir).
- Reports whether the NVIDIA runtime appears in the runtimes list.
- The GPU test run (`docker run --gpus all ...`) is **not** in this step. It pulls an image and uses the GPU, so it is a later, opt-in check.
- Fixtures: daemon running with NVIDIA runtime, daemon running without it, daemon not running.

**Verify:**
- Tests for each fixture.

**Commit:** `feat(collectors): add docker collector`

---

## Step 7: `kernel_log` collector

**Goal:** Find GPU and bus errors in the kernel log.

**Scope:**
- Reads `journalctl -k -b --no-pager` and falls back to `dmesg -T` if journalctl is missing. Both may need root. The collector reports "needs root" as a fact if it cannot read them.
- Extracts lines matching `nvrm`, `xid`, `aer`, `pcie`, and I/O errors from the SSD health page patterns.
- Keeps the matched lines as evidence (redacted later in step 16). Each Xid code is reported as its own fact.
- Fixtures: a log with Xid 79 (GPU fell off bus), a log with AER errors, a clean log.

**Verify:**
- Tests for each fixture, plus the root-missing case.

**Commit:** `feat(collectors): add kernel_log collector for NVRM, Xid, and AER`

---

## Step 8: `network` collector

**Goal:** Check DNS and outbound connectivity, without a shell.

**Scope:**
- DNS: resolve `vast.ai` with `socket.getaddrinfo`. Timeout-bounded.
- Outbound: TCP connect to `1.1.1.1:443` and to `8.8.8.8:53` (both with a timeout).
- Reads `/etc/resolv.conf` and reports the configured nameservers as facts.
- No ping and no HTTP call in this step. The `curl -4 ifconfig.me` check from the Vast docs is deferred (it sends the host's public IP to a third party, so it needs an explicit user choice).
- Ports (5 per GPU) are **not** checked in this step.

**Verify:**
- Tests with the DNS and TCP calls behind a fake resolver and fake socket. No real network needed.
- Manual: run on the Windows machine and on Linux, check the facts match what you expect.

**Commit:** `feat(collectors): add network collector for DNS and outbound reachability`

---

## Step 9: `vast_host` collector

**Goal:** Know whether the Vast daemon is running and what its log says.

**Scope:**
- `systemctl is-active vastai.service` and `systemctl show vastai.service -p ActiveState,SubState,NRestarts`. Reports restart loops.
- Reads the last 100 lines of `/var/lib/vastai_kaalia/kaalia.log` if the file is readable. Keeps only lines that match error patterns, not the whole log.
- Reports missing file and missing service as separate facts.
- Fixtures: active, inactive, failed, restart loop, missing unit.

**Verify:**
- Tests for each fixture.

**Commit:** `feat(collectors): add vast_host collector for vastai.service and daemon log`

---

## Step 10: `disk` collector

**Goal:** Storage facts from the SSD health page, read only.

**Scope:**
- Root free space (`psutil`), and Docker Root Dir size and free space.
- Drive type and transport from `lsblk -d -o NAME,ROTA,SIZE,MODEL,TRAN`.
- SMART overall health from `smartctl -H` (needs root; reported as "needs root" if not run).
- NVMe critical warning, spare, and media errors from `nvme smart-log` or `smartctl -x` (needs root).
- SATA reallocated, pending, and reported-uncorrect counts from `smartctl -x`.
- Does not run self-tests (`smartctl -t`). Those are writes and compete with client I/O.

**Verify:**
- Tests with fixtures for healthy NVMe, failing NVMe (media errors), healthy SATA, failing SATA (pending sectors), HDD.

**Commit:** `feat(collectors): add disk collector for space, SSD type, and SMART health`

---

## Step 11: Rule engine and first rules

**Goal:** Turn facts into findings, using rules stored as data.

**Scope:**
- Rules are TOML files shipped in the package (`rules/*.toml`). Each rule has an id, title, conditions on facts, severity, category, recommendation, and optional `fix_id`.
- Conditions are a small, fixed set: equals, not equals, less than, greater than, contains, missing, and/or. No code execution in rules.
- Rule loader validates the TOML with pydantic and rejects unknown condition types.
- First rules, from the verified requirements in PLAN.md:
  - DNS for `vast.ai` fails.
  - Outbound TCP fails.
  - Docker NVIDIA runtime missing.
  - `vastai.service` not active.
  - NVRM or Xid errors present in kernel log.
  - `nvidia-smi` fails.
  - GPU count below the count reported by the PCI bus. *(Only if a reliable source exists. Otherwise drop this rule.)*
  - CUDA below 11.8.
  - Root free space below 20 GB.
  - Docker storage below 200 GB.
  - Root not mounted read-write.
  - SMART overall health not PASSED.
  - NVMe critical warning not `0x00`, or media errors above 0.
  - SATA reallocated or pending sectors above 0.
  - Secure Boot enabled. *(Needs a new collector. Added here as a rule only if the collector exists. Otherwise, a later step.)*
- Thresholds live in the rule files with a comment naming the Vast page they come from.

**Verify:**
- Each rule has a passing and a failing fixture (tests).
- Loader rejects a malformed rule file (test).
- Rule evaluation is deterministic (same facts, same findings).

**Commit:** `feat(rules): add rule engine and first diagnosis rules`

---

## Step 12: `diagnose` command

**Goal:** The first useful command. Collect, evaluate, and print.

**Scope:**
- `vast-troubleshooter diagnose` runs all collectors, evaluates all rules, and prints findings sorted by severity.
- Each finding shows its summary, evidence, and recommendation.
- Output modes: human (Rich), `--json`.
- Flag `--allow-root-reads`: lets collectors run `sudo`-prefixed read commands. Default off. The tool prints which commands need root.
- Exit codes: 0 no critical findings, 1 critical findings, 2 tool error.
- No fixes in this step.

**Verify:**
- Tests with the fake runner: end-to-end diagnose run on a healthy fixture set gives no findings. On a failure fixture set (DNS broken, service inactive) gives the expected findings.
- Manual: run on Linux, check output.

**Commit:** `feat(cli): add diagnose command`

---

## Step 13: Rental detection

**Goal:** Decide, on evidence, whether a rental is active. Blocked until a real host or a faithful test host is available.

**Scope:**
- Investigate candidate sources on a real host (see blockers): daemon log, Docker containers, VM state.
- Write a short finding document `docs/rental-detection.md` with what was seen and the chosen method.
- Implement `rentals.py` with a `RentalState` result: `active`, `none`, or `unknown`. `unknown` blocks disruptive fixes.
- Add a fact `rentals` to the diagnose output.

**Verify:**
- Tests for each state using fixtures from the real host.
- Manual: confirm `unknown` on a host where detection is impossible, and that fixes are blocked.

**Commit:** `feat(rentals): add rental detection with active, none, and unknown states`

**Note:** Do not start this step on a guess. If no real host is available, the step waits.

---

## Step 14: Fix engine (dry run, confirm, undo)

**Goal:** The safe way to change the system. No fix is wired up yet.

**Scope:**
- `fixes/base.py`: a `Fix` with `id`, description, `requires_root`, `requires_no_active_rental`, `dry_run()` returning the exact commands, `apply()`, `undo_notes`.
- Fix engine checks, in order: fix is in the whitelist, root is allowed, rental state is `none` if required. Any failure stops the fix and says why.
- Dry run is the default. `apply` requires typing the fix id, not just "yes".
- Every run writes an action log entry (JSON lines) with time, fix id, commands, result, and undo notes.
- After `apply`, the affected collectors re-run and the finding is re-checked.

**Verify:**
- Tests with a test fix that records calls.
- Tests: rental state `active` blocks; `unknown` blocks; dry run changes nothing; confirmation mismatch changes nothing.

**Commit:** `feat(fixes): add fix engine with dry run, confirmation, and action log`

---

## Step 15: First fix set

**Goal:** Real fixes for the POC, chosen by the user (blocker: first fix set).

**Scope (proposed, to be confirmed):**
- `restart-vast-daemon`: `systemctl restart vastai.service`. Requires root and no active rental. Undo: none needed, the service restarts.
- `fix-dns-resolver`: restores a public resolver in the system resolver config. Requires root and no active rental. Keeps a backup of the old config for undo. *(Exact method depends on systemd-resolved vs static resolv.conf. Checked on a test host.)*

Not in the first set, because they are disruptive or unverified: controller reset (Vast docs step 3), driver changes, reboots.

**Verify:**
- Each fix: dry-run output tested; apply tested on a disposable Ubuntu VM (blocker: VM). Undo tested.
- Rental block tested with a simulated active rental.

**Commit:** `feat(fixes): add restart-vast-daemon and fix-dns-resolver`

---

## Step 16: Reports and redaction

**Goal:** A report the host can share with Vast Support, with secrets removed.

**Scope:**
- `report.py`: writes `report-<timestamp>.json` and `report-<timestamp>.md` to a chosen directory.
- Contents: host summary, findings, facts, actions taken (from the action log), rental state.
- Redaction, on by default: IPv4 and IPv6 addresses, hostnames, machine IDs, anything matching key or token patterns, and the user name in paths. `--no-redact` turns it off and prints a warning.
- `report` command: runs diagnose and writes the files. Nothing is uploaded.

**Verify:**
- Redaction tests with fixture text containing IPs, keys, and paths. No secrets remain in the output.
- JSON validates against the pydantic model.
- Markdown renders (manual check).

**Commit:** `feat(report): add JSON and Markdown report with redaction`

---

## Step 17: Docker image for Windows dev and test

**Goal:** Run the Linux tool in Docker on Windows, for development and testing only.

**Scope:**
- `Dockerfile` based on Ubuntu, installs the package with `uv`.
- `docker-compose.yml` or a documented `docker run` command. Mounts only what is needed. No `--privileged`.
- Documents what works in the container and what does not (GPU access through Docker Desktop is limited). Ties to the verification in this step.
- `README.md` gets a short "Run on Windows (dev/test)" section.

**Verify:**
- `docker build` succeeds on the Windows machine.
- `docker run ... vast-troubleshooter diagnose` runs and produces output.
- The GPU limits are written down with what was actually observed.

**Commit:** `feat(docker): add dev and test image for running on Windows`

---

## Step 18: POC end-to-end test

**Goal:** Prove the POC works, as defined in PLAN.md section "POC scope".

**Scope:**
- On a disposable Ubuntu VM, inject a known fault: break DNS resolution for `vast.ai`.
- Run `diagnose`. It must report the DNS finding.
- Run the DNS fix in dry-run, then apply with confirmation. Re-run `diagnose`. The finding must clear.
- Start a test container to simulate an active rental. Confirm the fix is blocked while it runs.
- Generate a report. Check it is redacted.
- Record the results in `docs/poc-results.md`.

**Verify:**
- Each of the above steps is recorded with actual output.
- Any failure is reported, not hidden.

**Commit:** `test(e2e): record POC end-to-end results`

---

## Step 19: Packaging and release

**Goal:** Installable with `uv`, with a clear license and version.

**Scope:**
- `uv build` produces a wheel and sdist. Include the rule TOML files and `LICENSE`.
- Install from the built wheel into a clean environment and run `vast-troubleshooter --help`.
- Version bumped to `0.1.0` in `pyproject.toml`, with a CHANGELOG entry.
- Checksums for the built artifacts.

**Verify:**
- Clean install works. `--help`, `version`, and `diagnose --json` run.

**Commit:** `chore(release): package v0.1.0 with checksums and changelog`

---

## Open items that can change this plan

- The support matrix (Ubuntu and NVIDIA driver versions) is not decided.
- Secure Boot and SSH checks need collectors that are not in the steps above yet. They can be added as a new step before step 11, if confirmed.
- The "Understanding Verification" page and the host setup guide have not been read. They may change thresholds or add checks.
- Rental detection (step 13) is blocked on real host access.
