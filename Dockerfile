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

# -- e2e: a disposable TEST MACHINE with systemd as PID 1 ------------------------------------------
# Used only by e2e/run_poc.sh for the end-to-end POC test (docs/poc-results.md).
# It runs as root with a real init so systemctl and resolvectl are real. It needs
# an elevated container (see e2e/run_poc.sh) and must NEVER be used on a real host.
# There is no Docker daemon in it: e2e/docker is a stand-in CLI for the rental check.
FROM ${UBUNTU} AS e2e
ENV container=docker \
    DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       systemd systemd-sysv systemd-resolved dbus iproute2 pciutils smartmontools \
       ca-certificates iputils-ping \
    && rm -rf /var/lib/apt/lists/* \
    # A container does not need these units; masking them speeds up boot.
    && systemctl mask getty.target console-getty.service systemd-logind.service \
       systemd-timesyncd.service apt-daily.timer apt-daily-upgrade.timer \
       motd-news.timer e2scrub_all.timer fstrim.timer 2>/dev/null; true
COPY --from=builder /opt/python /opt/python
COPY --from=builder /app/.venv /app/.venv
COPY e2e/docker /usr/local/bin/docker
COPY e2e/vastai.service /etc/systemd/system/vastai.service
COPY e2e/driver.py /opt/e2e/driver.py
RUN chmod 0755 /usr/local/bin/docker \
    && mkdir -p /var/lib/e2e /var/lib/vastai_kaalia /reports \
    && printf '%s\n' '2026-11-23 10:00:01 INFO stand-in daemon started' '2026-11-23 10:00:31 INFO heartbeat ok' > /var/lib/vastai_kaalia/kaalia.log \
    && ln -s /etc/systemd/system/vastai.service /etc/systemd/system/multi-user.target.wants/vastai.service
ENV PATH="/app/.venv/bin:${PATH}"
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]

# -- final: what a plain `docker build .` produces ---------------------------------------------
# Docker builds the LAST stage by default. This keeps that the hardened runtime image,
# never the root-running e2e test machine above.
FROM runtime AS final
