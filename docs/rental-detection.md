# Rental detection

Status: **interim, fail-closed. No real Vast host has been inspected.**

Several fixes (restarting the Vast daemon, changing DNS) can interrupt a paying
customer. Vast's own docs say not to reboot, stop containers, force-stop VMs,
delete instance data, or change NVIDIA drivers while rentals are active. So the
tool must know whether a rental is active, and must refuse when it does not know.

## What is known (from the Vast docs, checked 2026-11-23)

- The Machine Metrics page shows per-container states: "exists (stopped)",
  "running", and "running (vm)". So rentals can be containers or VMs, and a
  stopped container can still exist for a customer.
- The docs do **not** say that rentals run as Docker containers, name the
  daemon's process or containers, or describe the VM backend.
- The daemon is `vastai.service`. Its log is `/var/lib/vastai_kaalia/kaalia.log`
  (root-only). Its format is not documented.

## What the tool does now

`src/vast_troubleshooter/rentals.py` returns one of three states.

| State | Meaning | Disruptive fixes |
|---|---|---|
| `active` | Some source found evidence a workload may exist. | Blocked |
| `none` | **Every** required source looked, readably and within 30 seconds, and found none. | Allowed |
| `unknown` | Anything else. | Blocked |

Rules:

1. Positive evidence always wins over absence of evidence.
2. A source that cannot look (not installed, permission denied, timeout,
   truncated output, a crash) is `unknown`, never `none`.
3. `none` older than 30 seconds, or stamped in the future, is `unknown`. Fixes
   must check again immediately before they change anything.
4. A required source that did not report makes the state `unknown`.
5. Confirmation by the operator does not override `active` or `unknown`.

Sources:

| Source | Evidence | Status |
|---|---|---|
| `containers` | `docker ps -a --format {{.State}}` through the pinned local Docker socket. Any container in any state is a possible rental. Only the state is read: never names, ids, images, environment, or logs. | Implemented. Conservative: it can over-block (the host's own containers count), never under-block. |
| `vms` | Unknown. | **Stub: always `unknown`.** |

**Consequence:** because the VM source always says `unknown`, the overall state
can never be `none` today. Every disruptive fix is blocked in practice. That is
deliberate. Tests use injected sources to exercise the `none` path.

## What is needed to finish this

A real Vast host (or a faithful copy), sampled in three conditions: no rental, a
container rental, and, if offered, a VM rental. For each, please capture:

1. `docker ps -a --format '{{.State}}'` and, separately, whether any of the
   containers are the host's own.
2. How a VM rental shows up: for example `virsh list --all`, `ps` for `qemu`, or
   the Machine Metrics timeline. Any of these, with names and ids removed.
3. The last 50 lines of `/var/lib/vastai_kaalia/kaalia.log` during each
   condition, with ids, names, and addresses removed.
4. Whether the daemon exposes rental state anywhere else (a status file, a local
   socket, a CLI).

With that, the container source can be narrowed to identify real rentals, the VM
source can be written, and the daemon log can be added as a third source.

## Not solved by this module

A host-local check cannot stop Vast from assigning a new rental after the check.
Vast's driver guidance says a scheduled maintenance window neither unlists the
host nor clears instances. Until a way to verify an unlisted or maintenance
state is found, disruptive fixes should stay advisory on production hosts.
