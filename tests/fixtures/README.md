# Fixtures

Recorded command and file output used by collector tests.

Status: fixtures here were **written from the documented formats**, not captured
from a real host. Replace them with real captures when a host is available.
Real captures must be sanitized (no hostnames, IPs, serial numbers).

## gpu/

`nvidia-smi` and `lspci` output for the `gpu` and `driver` collectors. Cases:
`healthy_1gpu`, `healthy_8gpu`, `failed_driver`, `err_columns` (an `ERR!`, a
`[N/A]`, and a `[Unknown Error]` column), `fewer_gpus` (8 GPUs on the PCI bus,
7 in `nvidia-smi`). Written from the documented formats, **unverified against
real output**. Open question: which throttle-reason field name each driver
accepts (`clocks_event_reasons` vs `clocks_throttle_reasons`).

## docker/

`docker info --format '{{json .}}'` output for the `docker` collector, trimmed to
the fields the collector reads: `running_nvidia`, `running_no_nvidia`,
`daemon_down`, `permission_denied`, `daemon_down_observed`. The first four are
written from the documented format, **unverified against real output**.
`daemon_down_observed` is the shape actually seen on 2026-11-23 with Docker 29.9.3
(Windows client, pinned to a Unix socket that does not exist): exit code 1,
JSON on stdout with empty fields and `ServerErrors: null`, and the message
"Cannot connect to the Docker daemon ... Is the docker daemon running?" on
stderr only. So `ServerErrors` cannot be relied on; the collector also reads stderr.

## kernel_log/

Kernel log text for the `kernel_log` collector: `xid79_journal.txt`,
`aer_journal.txt`, `aer_severity_only_journal.txt`, `clean_journal.txt`
(`-- No entries --`), `xid79_dmesg.txt` (`dmesg -T` format, includes I/O errors and
a day-of-month padded with a space), `clean_dmesg.txt`. Written from documented
formats and the NVIDIA Xid / kernel AER message shapes, **unverified against
real output**. The fixtures contain a made-up hostname and process names on
purpose, so tests can prove they never reach a fact. Open questions: whether
`journalctl -g` exits non-zero on no match on the Ubuntu versions in the support
matrix, and the exact `NVRM: Xid` wording on newer drivers.

## network/

`/etc/resolv.conf` variants for the `network` collector: `resolv_systemd.conf`
(systemd-resolved stub), `resolv_public.conf`, `resolv_private_and_ipv6.conf`,
`resolv_empty.conf`, `resolv_junk.conf` (invalid entries that must be ignored).
Written from the documented format, **unverified against real files**. All
addresses are public documentation or well-known resolver addresses.

## vast_host/

`systemctl show vastai.service` output (`show_active`, `show_inactive`,
`show_failed`, `show_restart_loop`, `show_not_found`), the message a host without
systemd prints (`no_systemd`), and two daemon logs (`kaalia_clean.log`,
`kaalia_errors.log`). The `show_*` files are written from the documented
`systemctl show` format. **The kaalia.log format is invented**: Vast does not
document it and no real log has been seen, so the log tests only prove the
generic error-line counting works. The error log fixture contains a made-up
machine id, container name, instance id, and IP address on purpose, so tests can
prove none of them reach a fact. Real `kaalia.log` samples are needed.

## disk/

`lsblk -P` output (`lsblk_nvme`, `lsblk_mixed` with NVMe, SATA SSD, HDD, a USB stick
and a virtual disk, `lsblk_bad_name`), `df -P -B1` output (`df_root`,
`df_docker_own`, `df_docker_same`), `findmnt` options (`findmnt_rw`, `findmnt_ro`),
and trimmed `smartctl -x` output (`smart_nvme_healthy`, `smart_nvme_failing`,
`smart_sata_healthy`, `smart_sata_failing`, `smart_hdd`). Written from the
documented formats and the Vast SSD health page, **unverified against real
output**. The smartctl fixtures contain made-up serial numbers on purpose, so
tests can prove they never reach a fact.

## real_linux_container/

**Real output, captured 2026-11-23** inside an Ubuntu 24.04 container (arm64) on
Docker Desktop's WSL2 kernel. Unlike every other folder here, nothing in it was
written by hand. It is real Linux, but it is a container, not a host: no GPUs, no
systemd, virtual disks. Files: `lsblk.txt` (note the padded `MODEL` and the
`ROTA="1"` on virtual disks), `df_root.txt`, `findmnt_root.txt` (an overlay root
with a very long options string), `ip_route_default.txt`, `lspci_nvidia.txt`
(empty: no NVIDIA device), `uname_r.txt`, `os-release`, `proc_version.txt`,
`proc_uptime.txt`, `resolv.conf` (Docker's own), `tail_missing_log.txt` and
`dmesg_nonroot.txt` (the real error texts). Tested by
`tests/test_real_linux_captures.py`. Still needed: the same from a real host.
