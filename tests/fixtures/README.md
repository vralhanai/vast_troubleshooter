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
