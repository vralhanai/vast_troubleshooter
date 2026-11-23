# syntax=docker/dockerfile:1
#
# Dev and test image for running the Linux tool on a Windows machine.
# It is NOT a way to run the tool on a Vast host: a container cannot see the
# host's kernel log, systemd, drives, or rentals. See docs/docker-dev.md.
#
# Targets:
#   runtime (default)  the tool, as a non-root user, with the read-only tools it calls
#   test               the whole test suite, run on real Linux as a non-root user
#
# Base images are pinned by digest. To update them, see docs/docker-dev.md.

# ubuntu:24.04 (multi-arch index) and uv 0.13.9, looked up 2026-11-23
ARG UBUNTU=ubuntu:24.04@sha256:562111b88caa79c71391c0c4d3c2114a7d5c35915c20561fd893cb1e07929e6b
ARG UV=ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d
ARG PYTHON_VERSION=3.15

FROM ${UV} AS uv

# -- builder: a managed Python, and the project installed from the lock file -----------------
FROM ${UBUNTU} AS builder
ARG PYTHON_VERSION
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_PREFERENCE=only-managed
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN uv python install ${PYTHON_VERSION}
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
# --frozen: install exactly what uv.lock says. --no-editable: a real install.
RUN uv sync --frozen --no-dev --no-editable --python ${PYTHON_VERSION}

# -- runtime: no build tools, no package manager, not root ----------------------------------------
FROM ${UBUNTU} AS runtime
# The read-only tools the collectors call. There is no Docker CLI and no systemd:
# the container has no Docker socket and no init, so those checks report 'missing'
# or 'unsupported' instead of guessing.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates iproute2 pciutils smartmontools \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --user-group --create-home --home-dir /home/vast --shell /usr/sbin/nologin vast \
    && mkdir /reports && chown vast:vast /reports
COPY --from=builder /opt/python /opt/python
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER vast
WORKDIR /home/vast
ENTRYPOINT ["vast-troubleshooter"]
CMD ["--help"]

# -- test: the full test suite on real Linux, as a non-root user ------------------------------------
FROM builder AS test
RUN useradd --uid 10001 --user-group --create-home --home-dir /home/vast --shell /usr/sbin/nologin vast \
    && chown -R vast:vast /app
COPY --chown=vast:vast tests ./tests
USER vast
ENV UV_CACHE_DIR=/tmp/uv-cache \
    HOME=/home/vast
# Re-sync with the dev group (pytest, ruff) as the unprivileged user.
RUN uv sync --frozen --python ${PYTHON_VERSION:-3.15}
CMD ["uv", "run", "--frozen", "pytest", "-q"]
