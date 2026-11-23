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

With a fake mutation runner, the apply step failed and the undo step also failed with exit 7. The action log correctly recorded the undo error. The engine still returned `rolled_back=True` and added “rolled back” to its reasons, which makes the CLI claim the change was undone. The engine discards the return value from the rollback steps. See [engine.py](src/vast_troubleshooter/fixes/engine.py#L328).
