# Releasing, and how security fixes reach users

## Build a release (from a clean checkout)
```
git clone <repo> release && cd release && git checkout <tag>
uv sync --frozen            # exact versions from uv.lock
uv run --frozen pytest -q   # all tests must pass
uv build                    # dist/*.whl and dist/*.tar.gz
cd dist && sha256sum * > SHA256SUMS
```
Builds are reproducible: two builds from the same commit give identical hashes
(checked for 0.1.0). Verify a download with `sha256sum -c SHA256SUMS`.

Check before tagging: version in `pyproject.toml`, an entry in `CHANGELOG.md`, the
wheel contains `rules/*.toml` and `LICENSE`, and a fresh virtualenv can install the
wheel and run `vast-troubleshooter --help`, `version` and `diagnose --json`.

## Install
`uv tool install vast_troubleshooter-<version>-py3-none-any.whl` (verify the checksum first).

## Pinned inputs
- Python dependencies: `uv.lock` (use `--frozen`).
- Container base image and `uv`: pinned by digest in the `Dockerfile`. To update:
  `docker buildx imagetools inspect ubuntu:24.04`, replace the digest, rebuild, run the tests.

## How security fixes reach users
- Report a vulnerability privately to the maintainer (GitHub security advisory on the repository).
- A fix ships as a new patch version (`0.1.x`) with a `CHANGELOG.md` entry marked
  **Security**, a new tag, and new checksums. Users upgrade by installing the new wheel.
- Dependency or base-image vulnerabilities: update `uv.lock` or the digest, rerun the full
  tests, release a patch version.
- There is no auto-update and no network call from the tool; updating is the operator's action.
