# Vast Troubleshooter testing by Codex

Runs on Windows with Docker Desktop, Ubuntu 24.04 ARM64, and Debian 13 ARM64. The Linux containers used the WSL2 kernel. Test counts refer to the checkout available at each run.

## Initial system collector run

- `uv run vast-troubleshooter collect system` exited 0 on both Ubuntu and Debian. The OS ID, version, display name, kernel release, and uptime all reported `[ok]`. Ubuntu reported `ubuntu`, `24.04`, and `Ubuntu 24.04.5 LTS`; Debian reported `debian` and `13`.
- All 189 tests passed on both distributions. Ruff initially reported 25 `EXE002` errors because the Windows bind mount presented Python files as executable. Git records them as non-executable. Ruff passed on a Linux-side copy with those modes restored.
- The cached `uv` image lacked a shell and the binaries needed for managed Python discovery. A local image combining `uv` and Python enabled the Debian run. The Ubuntu run used a cached Ubuntu-based image with managed Python.

## Follow-up verification through Step 17

| Environment | Result |
| --- | --- |
| Ubuntu 24.04, Steps 14–15 snapshot | 561 passed. Ruff lint and formatting passed. |
| Ubuntu 24.04, full checkout through Step 17 | 756 passed. Ruff lint and formatting passed. |
| Windows, full checkout through Step 17 | 750 passed and six POSIX-only tests skipped. |
| Docker Compose test image | 710 passed and one repository-files module skipped. That module ran in the full checkout. |

- All nine collectors ran as an unprivileged Ubuntu user. The default network check left outbound probes disabled. Opting in connected to both configured TCP endpoints. The disk collector read the Linux container's filesystem and virtual disks. GPU data, the Docker daemon, Vast's service, and privileged logs were unavailable or limited in the basic container.
- `diagnose` produced human output and parseable JSON with matching exit codes. The normal user run returned 3 with three passes, 11 unknowns, and one skip. The root run returned 3 with five passes, nine unknowns, and one skip. The elevation notice went to stderr. A separate run with real `sudo -n` and one allowed read successfully read a root-only synthetic daemon log as an unprivileged user.
- Replacing the container's resolver with an unused loopback address produced the sole critical finding `network.dns_vast_ai_unresolvable` and exit 1. The resolver file was restored. The healthy default run reported the rental state as `unknown`. The CLI blocked the daemon restart, the DNS demo without its opt-in, and the opted-in DNS demo on an unsuitable container. An unknown fix ID also returned 1.
- Linux verified that the runner replaced an inherited untrusted `PATH` and that a timeout killed a spawned descendant. The POSIX tests covered `flock`, owner-only state files, and symlink refusal.
- Both Compose targets built. The runtime ran as UID 10001 with no effective capabilities, `no-new-privileges`, and no Docker socket. It identified itself as a container on a WSL2 kernel. Its intentionally read-only root produced `disk.root_not_writable` and diagnosis exit 1.
- The Compose runtime wrote JSON and Markdown reports with mode `0600`. A newly created report directory had mode `0700`. The JSON validated against the report model. A seeded API secret, IP address, and raw field were absent from the files and terminal preview.
- `uv build` produced a wheel and source distribution. The wheel included six rule TOML files and the license. A clean wheel installation ran `--help`, `version`, and `diagnose --json` successfully. The diagnosis returned the expected incomplete status for that container.
- Intermediate failures cleared on rerun. Windows pytest initially hit permission errors in its default temporary directory, then passed with a fresh `--basetemp`. The first Compose build captured an in-progress fixture update and failed two collector tests. A later snapshot failed collection because Docker configuration tests expected checkout files inside the test image. The final image skipped that module and passed the rest. Executable-mode warnings and formatting failures from intermediate images cleared in the full Linux-side checkout.

### Confirmed failure in rollback reporting

With a fake mutation runner, the apply step failed and the undo step also failed with exit 7. The action log correctly recorded the undo error. The engine still returned `rolled_back=True` and the CLI claimed the change was undone. Commit `8f35b60` was added during the next round to record failed rollback separately.

## Independent Step 18 fault and repair run

Ubuntu 24.04 ARM64 ran systemd as PID 1 in a private cgroup namespace. Network and filesystem changes were confined to the disposable container. The run used a stand-in Vast service and Docker CLI, plus injected VM evidence for engine tests. The repair commands used real `systemd-resolved`, `resolvectl`, and `systemctl`. The first boot attempt failed because Docker mounted the cgroup path read-only. Mounting a writable cgroup filesystem within the private namespace and bind-mounting it over that path inside the container enabled the run. No host files, sockets, or cgroups were bind-mounted into the container.

| Scenario | Observed result |
| --- | --- |
| Healthy DNS baseline | `vast.ai` resolved. `diagnose --json` reported zero findings and exited 3. Eight checks were unknown. |
| Remove all upstream DNS servers | The lookup failed. Diagnosis exited 1 with only `network.dns_vast_ai_unresolvable`. |
| Demo flag and rental checks | The CLI refused the DNS repair without the demo flag. A simulated running container produced `active` and blocked apply even with injected VM evidence of `none`. Removing it produced `unknown` in the normal CLI, which still blocked apply. Both refusals exited 1. |
| DNS dry run and apply | The test harness planned the two `resolvectl` commands without modifying the link. With injected evidence of no rentals, apply exited 0, lookup recovered, and the finding cleared. Diagnosis returned to exit 3 with zero findings. |
| Manual DNS undo | `resolvectl revert eth0` restored the empty per-link configuration. With the upstream still missing, the DNS finding returned and diagnosis exited 1. |
| Forced DNS repair failure | Blackhole routes made both public resolvers unreachable. The repair exited 4. Both rollback commands returned `ok` in the action log and the link returned to its original empty DNS configuration. |
| Stop the stand-in `vastai.service` | Diagnosis exited 1 with only `vast_host.service_not_active`. The normal CLI refused to apply the restart with unknown rentals. The harness dry run left the unit inactive. Harness apply exited 0, the service became active, and the finding cleared. A second apply was blocked because the check already passed. |
| Report after the repairs | The JSON validated. Both report files were mode `0600` in a mode `0700` directory. The seeded secret, IP, raw field, and test hostname were absent from the JSON, Markdown, and terminal preview. |

The Windows suite in this round returned 768 passed and six POSIX-only skips on the Step 18 snapshot. The project also has a separate [Step 18 transcript](docs/poc-transcript.txt). The results above come from the independent run.

### Confirmed failure restoring existing per-link DNS settings

I set `eth0` to a pre-existing runtime DNS server (`192.0.2.1`) and search domain (`corp.invalid`), then blackholed the original server and both repair targets. Diagnosis found the DNS fault and the harness accepted the setup for dry run. Apply exited 4 and reported “rolled back.” Afterwards, both `resolvectl dns eth0` and `resolvectl domain eth0` were empty. The repair lost both existing settings. This differs from the earlier status bug because the revert command itself succeeded. The precheck accepts this configuration and the undo command resets the link without restoring its previous settings. See [definitions.py](src/vast_troubleshooter/fixes/definitions.py#L92).

### Clean checkout verification

- Ubuntu ran **781 tests** from a Git archive of `8f35b60`, including regressions for failed and timed-out rollback commands. Ruff lint and formatting passed. This confirms the separate failure-reporting fix in the clean snapshot.
- The lockfile installed with `uv sync --locked`. Two separate builds of `8f35b60` produced matching SHA-256 checksums for the wheel and source distribution. Both archives contained the six rule files and the license. A fresh wheel environment using offline resolution could not resolve `pydantic` from its cache. The registry-backed retry installed the wheel successfully, and `--help`, `version`, and `diagnose --json` ran outside the checkout. Diagnosis exited 3 and its JSON carried the same code.

### Remaining coverage

A standalone Ubuntu VM, live Vast rental evidence, an actual NVIDIA GPU, and an amd64 image run remain unverified in these sessions. The successful applies used injected no-rental evidence in the test harness. The normal CLI correctly refused to apply while its production VM source returned `unknown`.
