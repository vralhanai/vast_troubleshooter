# Changelog

Format: [Keep a Changelog](https://keepachangelog.com). Versions follow [SemVer](https://semver.org).

## [0.1.0] - 2026-11-24

First release: a proof of concept. Tested in Docker and on real systemd, **never on a
real Vast host**; it cannot yet apply a fix to a live host (see `BACKLOG.md`).

### Added
- `diagnose` (human and `--json`), `collect <name>`, `fix` (dry run by default, typed
  confirmation), `report` (redacted JSON and Markdown), `version`.
- Collectors: system, gpu, driver, docker, kernel_log, network, vast_host, disk, rentals.
- 16 rules (offline, unverified, performance) with sources and check dates.
- Fixes: `restart-vast-daemon`, and the demo-only `fix-dns-resolver`.
- Fail-closed rental detection: a fix needs proof that no rental is active.
- Hardened Docker image (digest-pinned base, non-root, read-only root) for development.

### Safety properties
- Read-only by default; commands come from an allowlist; unreadable input is never
  reported as healthy; fixes re-check state right before changing it, log first, judge
  success by observation, and roll back on failure.
- Reports are allowlisted, redacted with no off switch, and written owner-only.

### Known limits
See `BACKLOG.md` and `TestingDoneByClaude.md` (33 review findings, all fixed except one
design decision recorded in the backlog).
