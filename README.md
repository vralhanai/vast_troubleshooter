# Vast Troubleshooter

An independent, local, command-line diagnostic and repair assistant for [Vast.ai](https://vast.ai) hosts. It inspects a Linux host machine, explains what is wrong, and (with consent) fixes it.

> Status: **Design phase, POC next.** No working code yet. See [PLAN.md](PLAN.md) for the three-level plan.

## 1. Problem

Vast.ai hosts rent their GPUs to customers. When a machine goes **offline**, becomes **unverified** (de-listed from search), or performs badly, the host loses rentals. Diagnosing the cause means checking many layers at once: NVIDIA driver and GPU state, Docker and its GPU runtime, the Vast host service, networking and open ports, PCIe bus health, storage health, kernel logs, and system resources. Hosts often have to do this by hand, from scattered docs and Discord threads, and Vast does not offer hands-on support for it: testing and troubleshooting are the host's responsibility.

This tool automates that first-line diagnosis and gives a clear, prioritized answer: *what is wrong, why it matters, and what to do next.*

## 2. Goals

- Diagnose the most common reasons a Vast host is **offline**, **unverified**, or **underperforming**.
- Cover all relevant categories: GPU, driver/CUDA, Docker, CPU, memory, disk/SSD, PCIe, network and ports, kernel and system logs, and the Vast host service.
- Explain each finding in plain language, with evidence and a suggested fix.
- **Diagnose and fix:** apply safe, confirmed fixes for well-understood problems.
- Produce a report the host can keep or attach to a support request.

## 3. Non-goals

- **No Vast.ai API calls.** The tool works offline from the host's own state. It does not read Vast internal data, databases, or private or public Vast APIs.
- No web UI for now. The interface is the command line.
- Not a monitoring or alerting service. It runs when the host runs it.
- Does not read customer workload data or files inside rented containers.
- Does not replace Vast.ai's own verification or support decisions.
- Windows is not a host platform. Vast does not accept Windows hosts. The Windows Docker setup exists only for development and testing of this Linux tool.

Note on "offline": this means no Vast API. Network checks (DNS, outbound reachability, speed test) still use the internet, because a host's connectivity is part of what the tool diagnoses.

## 4. Scope and requirements

| Area | Requirement |
|---|---|
| Target host OS | **Ubuntu Linux**. This is where the tool runs in production. |
| Development and test on Windows | The Linux tool runs **inside a Docker container** on Windows. Not a host platform. |
| Interface | **CLI only** for now. |
| Issue coverage | "All and more": GPU, CUDA/driver, Docker, CPU, RAM, disk/SSD, PCIe, network, ports, kernel logs, Vast host service. Coverage grows over the milestones. |
| Actions | **Diagnose and fix.** Every fix needs explicit confirmation. |
| Report | Yes. A shareable, redacted report (for example, for Vast Support). |
| Distribution | **`uv` package**, plus a Docker image for the Windows dev/test setup. |
| Language | **Python**. |
| License | **MIT**. |

### Known issue catalogue (starting point, to be verified)

Based on public Vast.ai host documentation ([machine offline](https://docs.vast.ai/host/machine-offline), [verification stages](https://docs.vast.ai/host/verification-stages), [self-test](https://docs.vast.ai/host/how-to-self-test), [SSD health](https://docs.vast.ai/host/ssd-health)):

- **Offline:** broken DNS (cannot resolve `vast.ai`), host service not running, GPUs not detected, NVIDIA Xid / NVRM / PCIe AER errors in kernel logs, host daemon stuck (controller reset needed).
- **Unverified:** CUDA below 11.8, too few forwarded ports (5 per GPU), network instability, low bandwidth, insufficient PCIe bandwidth, failing storage, `nvidia-smi`/NVML failures, container device-init errors, a kernel with a known exploited vulnerability, a driver below Vast's minimum for its GPU generation (Vast's driver table). See [PLAN.md](PLAN.md#verified-requirements-vast-docs-checked-2026-11-23).
- **Underperforming:** thermal throttling, low clocks, memory errors, slow disk, slow network.

## 5. Proposed tech stack

*Proposal only. Nothing below is final until confirmed.*

- **Language:** Python (project currently pins `requires-python = ">=3.15"`).
- **Packaging:** `uv` with `pyproject.toml`.
- **Host metrics:** `psutil`; GPU via `nvidia-smi` output or `pynvml`.
- **CLI and output:** `typer` (or `click`) for commands, `rich` for readable terminal output, JSON export for machines.
- **Data models:** `pydantic` for check results and rule definitions.
- **Rules:** diagnosis rules stored as data (YAML/TOML) so new issues can be added without code changes.
- **Docker image:** runs the same CLI on Windows for development and testing.
- **Tests:** `pytest`, with recorded command output fixtures (no real GPU needed for most tests).

## 6. Security guidelines

1. **Read-only by default.** Every check only reads. Any change to the system requires an explicit flag and an interactive confirmation, and the default answer is "no".
2. **Never disrupt paying customers.** No reboot, container stop, VM force-stop, or data deletion while a rental is active, unless the host explicitly confirms. Rental detection is a requirement. Proposed method: check for running Docker containers on the host, since rentals run as containers (to be verified). If the check cannot run, the disruptive fix is blocked.
3. **Local-first data handling.** Reports are generated and stored locally. Nothing is uploaded unless the user runs an explicit export step.
4. **Redaction.** Reports redact API keys, tokens, IP addresses, hostnames, and machine IDs by default. Redaction can be turned off explicitly.
5. **No customer data.** The tool never opens container file systems, volumes, or instance data.
6. **Secrets handling.** The tool needs no API key, because it does not call Vast. If one is ever needed, it is read from an environment variable or a file with restricted permissions, and never printed or logged.
7. **Least privilege.** Commands that need root (`dmesg`, `smartctl`, `systemctl`) are identified individually. The tool explains why each one needs elevated rights. The Docker container gets only the mounts and devices it needs, and avoids `--privileged` where possible.
8. **Untrusted input.** Logs, command output, and any remote content are parsed as data. Never passed to a shell as a string.
9. **Fixes are whitelisted.** Each fix is a named, reviewed action with a dry-run mode and a description of how to undo it.
10. **Supply chain.** Pinned dependencies, lock file committed, release artifacts with checksums.

## 7. Documentation

- [PLAN.md](PLAN.md): three-level plan. Level 1 is the system design. Level 2 is the detailed specification. Level 3 is the working system, starting with the POC.
- [STEPS.md](STEPS.md): the step-by-step build plan with progress checkboxes.
- [BACKLOG.md](BACKLOG.md): what was skipped, what is unverified, and what to do once a real Vast host, an Ubuntu machine, or an API key is available.
- [docs/rental-detection.md](docs/rental-detection.md): how rental detection works today and what is needed to finish it.
- [LICENSE](LICENSE): MIT.

## 8. Decisions and open questions

### Decided

| # | Topic | Decision |
|---|---|---|
| 1 | Windows | Vast does not accept Windows hosts. The Linux tool runs in Docker on Windows for dev/test only. |
| 2 | Vast API | Offline. No Vast API calls. |
| 3 | Fixes | Diagnose **and** fix, with confirmation. |
| 4 | Rental detection | Yes, the tool detects active rentals and blocks disruptive actions. |
| 5 | Interface | CLI first. |
| 6 | Report | Yes, a shareable redacted report. |
| 7 | Distribution | `uv` package, plus a Docker image for dev/test on Windows. |
| 8 | Network tests | Speed tests against third-party servers are acceptable. |
| 10 | License | MIT. |

### Still open

9. **Support matrix:** Which Ubuntu versions and NVIDIA driver versions must be supported? *Not decided yet.*

### POC

The current goal is a **proof of concept (POC)** CLI. The POC scope is defined in [PLAN.md](PLAN.md#poc-scope).
