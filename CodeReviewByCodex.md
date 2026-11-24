# Vast Troubleshooter code review

Reviewed commit `8f35b60`, including the CLI, collectors, rule engine, rental detection, repair engine, reports, command runner, Docker targets, and Step 18 test harness. The highest-priority issues concern recovery after a partial repair, keeping the applied command tied to what the operator confirmed, and keeping host identifiers out of shareable output.

## High priority

### P1. An action-log write failure can interrupt a repair after mutation and skip rollback

The engine writes `apply_started` before mutation, but writes each command result after the command has run. An I/O error at that point escapes the engine and the CLI. I injected an `OSError` after the first mutation with a registered undo step. Only that first mutation ran. The engine skipped rollback and postcondition checks and returned no `FixOutcome`. A separate final-log write can also turn a completed repair into an unhandled exception. Keep the initial audit write mandatory. Once mutation starts, handle later audit failures explicitly, stop advancing the repair, observe the machine, and attempt the applicable recovery. Add fault-injection tests after the first mutation and during rollback. [Engine apply and audit writes](src/vast_troubleshooter/fixes/engine.py#L251) · [Command logging](src/vast_troubleshooter/fixes/engine.py#L373)

### P1. A repair can apply to a different target than the one displayed before confirmation

The CLI previews a plan, asks for the fix ID, and calls the engine again without passing the preview. The second call discovers its own operands. I ran the normal CLI with a fake runner and a changing route. It displayed `eth0`, accepted confirmation, mutated `eth1`, and exited 0. The final recheck validates the problem and rental state, but does not bind execution to the displayed operands. Carry the previewed target and arguments into apply, rediscover them under the lock immediately before mutation, and require fresh confirmation if they differ. The DNS demo already discovers a dynamic target. The current rental gate still blocks both shipped fixes through the normal CLI. [Confirmation flow](src/vast_troubleshooter/cli.py#L339) · [Operand discovery and final checks](src/vast_troubleshooter/fixes/engine.py#L130)

### P1. The DNS demo accepts existing settings and can erase them on rollback

The DNS precheck verifies that systemd-resolved is active and that the resolver file has one loopback nameserver. It never establishes that resolved owns that stub or that the default link has no existing DNS or search domains. On real systemd-resolved, a forced repair failure cleared both a pre-existing per-link DNS server and a search domain, and the CLI said the change was rolled back. The undo command succeeded at resetting the link. Commit `8f35b60` fixes the separate case where the rollback command itself fails. Refuse nonempty or ambiguous link configuration unless the tool can snapshot the supported settings, recheck that snapshot before apply, and verify that rollback restored it. [DNS precheck and rollback](src/vast_troubleshooter/fixes/definitions.py#L92) · [Independent test results](TestingDoneByCodex.md)

### P1. Shareable JSON and reports can retain identifiers from arbitrary host strings

The Docker collector exports the raw Docker root directory as a normal fact value. The shareable model permits it and `diagnose --json` emits it without redaction. The report redactor also leaves arbitrary custom path segments untouched. I used a synthetic customer label inside a Docker path and found it in both outputs. The second redaction pass checks whether the same patterns would change the rendered result, so values missed by those patterns pass both checks. Define exportable values by fact key and purpose. For Docker storage, the shareable diagnosis can keep the capacity and filesystem facts while retaining the full path only in local collection output. Apply the same export policy to report evidence, which currently reads directly from the internal facts. [Docker path fact](src/vast_troubleshooter/collectors/docker.py#L126) · [Diagnosis JSON](src/vast_troubleshooter/cli.py#L171) · [Report evidence](src/vast_troubleshooter/report.py#L233)

## Correctness and reliability

### P2. One readable drive can make SMART checks pass for unreadable drives

The disk collector emits all aggregate counters whenever one SMART health line was readable. Other drive failures and absent protocol-specific attributes leave those counters at zero. A healthy SATA fixture plus a timed-out NVMe query produced `PASS` for both overall SMART health and NVMe health. Track coverage separately for each diagnostic. Preserve positive evidence of failure from any drive and require complete applicable evidence before reporting a pass. Include mixed success, timeout, and truncated SMART output in the regression tests. [SMART aggregation](src/vast_troubleshooter/collectors/disk.py#L283) · [Disk rules](src/vast_troubleshooter/rules/disk.toml#L49)

### P2. A truncated kernel log can pass the critical Xid 79 check

The collector exposes `kernel_log.truncated`, but the rules ignore it. A truncated fixture containing Xid 31 and no observed Xid 79 produced a pass for the critical Xid 79 check. Keep a confirmed Xid 79 actionable. When that code has not been seen and capture was incomplete, return unknown or obtain a bounded query for that event before passing the check. [Kernel log facts](src/vast_troubleshooter/collectors/kernel_log.py#L143) · [Xid 79 rule](src/vast_troubleshooter/rules/kernel_log.toml#L18)

### P2. Diagnosis omits an observed Docker daemon outage

The collector can return `docker.daemon_running=False [ok]`, but the only Docker rule checks NVIDIA runtime registration. I fed the daemon-unavailable result into the shipped rules and got an unknown runtime check with no Docker finding. Add a daemon-availability rule for a verified negative observation and gate the runtime check on a running daemon. Keep permission failures and incomplete observations unknown. [Docker failure classification](src/vast_troubleshooter/collectors/docker.py#L63) · [Docker rules](src/vast_troubleshooter/rules/docker.toml)

### P2. A failure to launch nvidia-smi is reported as a driver failure

The runner uses `ERROR` for both launch failures and nonzero process exits. The driver collector treats all remaining errors as evidence of a broken driver. I supplied a launch-permission failure with no child exit code and got `driver.nvidia_smi_responding=False [ok]`, which feeds a critical finding. Give launch failures a separate status or require evidence that the child ran before assigning the diagnostic failure. [Runner launch errors](src/vast_troubleshooter/runner.py#L228) · [Driver classification](src/vast_troubleshooter/collectors/driver.py#L55)

### P2. An empty packaged rule directory can produce an all-clear

The loader accepts an empty directory and returns an empty list. The diagnosis then exits 0 with zero checks. I verified this with a temporary empty directory. Directory and decoding errors also escape the CLI's current rule-file error handler. Treat an empty default rule set and resource I/O or decoding failures as tool errors. Keep a regression test for an incomplete installation. [Rule loader](src/vast_troubleshooter/rule_engine.py#L215) · [CLI rule error handling](src/vast_troubleshooter/cli.py#L198)

### P2. A timeout can still leave the runner waiting indefinitely

After the configured timeout, the runner attempts a kill and calls `proc.wait()` with no bound. The termination helper discards permission and other OS errors. A process that cannot be terminated promptly can therefore block the CLI beyond its advertised timeout. Give the cleanup wait a separate deadline and surface that termination could not be verified. Exercise the failed-termination path with a fake process and verify the real elevated path on Linux. [Timeout and termination](src/vast_troubleshooter/runner.py#L250) · [Kill helper](src/vast_troubleshooter/runner.py#L324)

### P2. Report write failures can leave a single output file behind

The file helper creates the destination before writing and syncing. If the Markdown sync fails, `write_report` removes the JSON but leaves the Markdown. A sync failure on the first file can leave the JSON behind. I injected a failure on the second sync and confirmed the lone Markdown file. Clean up each newly created file if its own write or sync fails, account for short writes, and test failures after creation rather than only failures opening a path. [Private file creation](src/vast_troubleshooter/securefs.py#L33) · [Report pair creation](src/vast_troubleshooter/report.py#L421)

### P2. Report and repair storage do not verify ownership of existing paths

The report directory check rejects group or world write permissions, but accepts a directory owned by a different user. That owner can still modify the directory. The repair state helper also accepts existing lock and log files without checking owner, mode, or file type. On POSIX, validate the directory and open file descriptors against the expected owner and mode. Keep file creation relative to the validated directory so later operations use the same directory. This matters most when the tool runs as root or writes to a caller-supplied report path. [Report directory checks](src/vast_troubleshooter/securefs.py#L15) · [Repair state files](src/vast_troubleshooter/fixes/storage.py#L36)

### P2. The Step 18 script records failures without failing the test run

The E2E helpers print exit codes through `tee` and return the result of logging. Several probes pipe the CLI into `head` or a parser and lose its real exit status. The script ends by printing a completion message, so a failed repair or failed leak check can still produce exit 0. Use explicit expected exit codes and assertions on finding IDs, service and resolver state, and redaction. Also create a unique container name and remove it only after a successful create. The current startup cleanup force-removes a fixed name before the run. The private-cgroup boot from the independent test is another way to avoid the current writable host cgroup mount. [E2E helpers and cleanup](e2e/run_poc.sh#L10) · [Independent container test](TestingDoneByCodex.md)

## Release readiness

- The standard Compose runtime has an intentionally read-only root. It reported `disk.root_not_writable` as critical and exited 1 in the earlier Docker test. The environment banner is helpful for humans, but automation still receives a host-style failure for an expected container setting. Mark the host filesystem check unavailable in a known container and retain the mount observation as a fact. [Compose configuration](docker-compose.yml#L20) · [Root filesystem rule](src/vast_troubleshooter/rules/disk.toml#L35)
- The README still describes a design phase with no working code, and the package metadata says “Add your description here.” Before release, document the real CLI, exit codes, containers, and the production rental gate. Run lint, formatting, and the full-checkout test suite in Linux and Windows CI. The Compose test image skips repository-level Docker tests by design, so it cannot replace the full-checkout job. [README](README.md#L5) · [Package metadata](pyproject.toml#L1) · [Docker configuration tests](tests/test_docker_files.py#L10)
- Production rental detection is deliberately fail-closed because the VM source remains a stub. Keep the live repair gate until rental evidence is validated on an actual host. A local lock cannot stop a new assignment by Vast. The Ubuntu container tests exercised real resolver and service commands using stand-ins and injected rental evidence. [Rental evidence implementation](src/vast_troubleshooter/rentals.py#L94) · [Rental design notes](docs/rental-detection.md)

## Verification

- Current review run on Windows. **775 passed**, six POSIX-only skips, Ruff lint and formatting clean.
- Prior Linux run from a clean archive of the same commit. **781 passed**, Ruff lint and formatting clean, wheel and source distribution built, and the installed wheel ran independently of the checkout.
- The targeted reproductions used local fixtures, fake command runners, and temporary directories. The real resolver rollback and Compose behavior came from the preceding disposable Linux runs, documented in [TestingDoneByCodex.md](TestingDoneByCodex.md). AMD64, an NVIDIA host, and real Vast rental evidence were outside the verified coverage.
