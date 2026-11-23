# Running on Windows with Docker (dev and test only)

Vast does not accept Windows hosts, so this tool targets Ubuntu. This page is
about running the **Linux** tool in a container on a Windows development machine,
to build and test it. **A container is not a Vast host.** Do not use it to
diagnose a real host.

Everything below was run on 2026-11-23 on a Windows 11 ARM64 laptop
(Snapdragon X Elite, Qualcomm Adreno GPU, **no NVIDIA GPU**) with Docker Desktop
29.9.3 (WSL 2 backend), so the images were built for `linux/arm64`.

## Quick start (PowerShell)

```powershell
# Build and run the tool, hardened (see "What the sandbox blocks")
docker compose run --rm vast-troubleshooter version
docker compose run --rm vast-troubleshooter diagnose
docker compose run --rm vast-troubleshooter report --output-dir /reports

# Run the whole test suite on real Linux, as a non-root user
docker compose run --rm tests
```

Without Compose:

```powershell
docker build --target runtime -t vast-troubleshooter:dev .
docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges:true `
  --pids-limit 256 --memory 512m `
  --tmpfs /tmp:rw,noexec,nosuid,size=16m `
  --tmpfs /home/vast:rw,noexec,nosuid,uid=10001,gid=10001,size=16m `
  vast-troubleshooter:dev diagnose

docker build --target test -t vast-troubleshooter:test .
docker run --rm --cap-drop ALL --security-opt no-new-privileges:true vast-troubleshooter:test
```

Reports written to the `reports` named volume stay inside Docker. To read one:
`docker run --rm -v vast-troubleshooter-dev_reports:/r:ro alpine cat /r/<file>`
(the volume prefix is the Compose project name).

## What the sandbox blocks

The runtime image runs as `vast` (uid 10001). The Compose file adds: a read-only
root filesystem, **all capabilities dropped**, `no-new-privileges`, a process and
memory limit, and `noexec` temporary folders. It deliberately does **not** use:

- the Docker socket (so the container cannot control Docker),
- host folder mounts (reports go to a named volume, not a host path),
- `--privileged`, added capabilities, or host PID, network, or IPC namespaces.

`tests/test_docker_files.py` fails if any of these is added.

Why a named volume and not a host folder: the report writer refuses an output
directory that other users can write to. A Windows folder mounted into a Linux
container shows as world-writable, so it would be refused.

## What works and what does not

Observed by running every collector in the hardened container:

| Collector | Result in the container | Why |
|---|---|---|
| `system` | **Works.** Reads the real Ubuntu 24.04 `os-release`, kernel, uptime. Reports `container on a WSL2 kernel`. | Real files. |
| `network` | **Works.** DNS resolves, `/etc/resolv.conf` read (Docker's own resolver `192.168.65.7`). `resolved_active` is `missing`. | No systemd. |
| `disk` | **Partly.** `lsblk`, `df`, `findmnt` work. The 5 virtual disks all report `rotational = True`. No SMART (virtual disks have no transport). `root_mounted_rw` is `False` when the container is run `--read-only`. | The container's disks, not a host's. |
| `gpu`, `driver` | `nvidia-smi` is not found. `lspci` finds no NVIDIA device (`pci_count = 0`). | No NVIDIA GPU here. |
| `docker` | `missing`. | No Docker CLI and no socket, on purpose. |
| `kernel_log` | `needs_root`. | The tool runs as a normal user. |
| `vast_host` | `missing` / `needs_root`. | No systemd, no Vast daemon. |
| `rentals` | `unknown`. | Always `unknown` today (see `rental-detection.md`). |
| `fix`, `report` | Fixes are blocked (not root, no systemd). `report` works into `/reports`. | |

**The tool now says it is in a container.** `diagnose` prints
`Environment: container on a WSL2 kernel. These results describe this
environment, not a Vast host.` The report carries the same label in its host
section. This was added because a plain run reported a CRITICAL "root filesystem
is mounted read-only" finding. That was true for the container and misleading for
anyone reading it as a host result.

## GPU passthrough (optional, and limited)

```powershell
docker compose -f docker-compose.yml -f docker-compose.gpu.yml run --rm vast-troubleshooter collect gpu
```

Needs: a Windows machine with an **NVIDIA** GPU, the NVIDIA driver for WSL, and
Docker Desktop's WSL 2 backend. What comes back is a container / WSL observation:
it shows `nvidia-smi` works through Docker Desktop. It says nothing about PCIe,
kernel logs, the Vast daemon, or rentals.

**Observed on this machine (no NVIDIA GPU):**

```
docker run --rm --gpus all vast-troubleshooter:dev collect gpu
docker: Error response from daemon: ... error running prestart hook #0: exit status 1 ...
nvidia-container-cli: initialization error: WSL environment detected but no adapters were found
```

Docker Desktop still lists an `nvidia` runtime as registered, so the tool's
`docker.nvidia_runtime_present` fact would read `True` here while no GPU works.
That is why the rule text says registration alone does not prove a GPU container
can start. GPU passthrough with a real NVIDIA GPU has **not** been tested.

## The test image: real Linux, first time

`docker compose run --rm tests` runs the full suite inside Ubuntu 24.04 as a
non-root user. On 2026-11-23 it gave **699 passed, 0 skipped**, including the 6
POSIX-only tests that Windows skips (owner-only file modes, refusing symlinks,
refusing a world-writable directory). Caveats: it is a container, the CPU is
arm64, and the tests use fakes for hardware, so it proves the Linux code paths
work, not that real hosts behave as the fixtures assume.

Real command output captured in the container is in
`tests/fixtures/real_linux_container/` and is tested by
`tests/test_real_linux_captures.py`.

## Keeping the images safe

- **Base images are pinned by digest** (`ubuntu:24.04` and `uv 0.13.9`, looked
  up 2026-11-23). To update: `docker buildx imagetools inspect ubuntu:24.04` and
  copy the `Digest:` line into the `ARG UBUNTU=` line of the `Dockerfile`; do the
  same for `ghcr.io/astral-sh/uv:<version>`. Rebuild, run `docker compose run
  --rm tests`, and commit.
- **Python 3.15 comes from `uv`**, not from Ubuntu (which ships an older Python).
  Dependencies are installed with `--frozen`, exactly as locked.
- The runtime image has no compiler, no package manager, and no tests. Its only
  extra tools are `iproute2`, `pciutils`, and `smartmontools`.
- There is no automatic update cadence yet. See `BACKLOG.md`.
