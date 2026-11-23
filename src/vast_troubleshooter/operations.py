"""The registry of read-only operations collectors may run.

Add an operation here only with a collector that needs it. Nothing in this
registry may change the system.
"""

import re
from collections.abc import Mapping

from vast_troubleshooter.runner import Operation

# nvidia-smi --query-gpu fields, in output order after `index`.
# (query name, fact key suffix, kind, unit)
GPU_QUERY_FIELDS: tuple[tuple[str, str, str, str | None], ...] = (
    ("name", "name", "str", None),
    ("memory.total", "memory_total", "int", "MiB"),
    ("temperature.gpu", "temperature", "int", "C"),
    ("power.draw", "power_draw", "float", "W"),
    ("clocks.sm", "clock_sm", "int", "MHz"),
    ("clocks.mem", "clock_mem", "int", "MHz"),
    ("ecc.mode.current", "ecc_mode", "str", None),
    ("pcie.link.gen.current", "pcie_gen", "int", None),
    ("pcie.link.width.current", "pcie_width", "int", None),
)

# Lines worth reading: NVIDIA driver (incl. Xid), PCIe AER, and storage I/O errors.
KERNEL_LOG_PATTERN = "NVRM|AER:|PCIe Bus Error|I/O error|EXT4-fs error"

_CSV = "--format=csv,noheader,nounits"
_QUERY = "--query-gpu=index," + ",".join(f[0] for f in GPU_QUERY_FIELDS)

_OPERATIONS = (
    Operation(id="system.uname_release", executable="uname", args=("-r",)),
    Operation(id="gpu.query", executable="nvidia-smi", args=(_QUERY, _CSV), timeout=20),
    # Newer drivers call this field "clocks_event_reasons"; older ones
    # "clocks_throttle_reasons". An unknown field fails the whole query, so
    # they are separate operations and the collector tries both.
    Operation(
        id="gpu.throttle_event",
        executable="nvidia-smi",
        args=("--query-gpu=index,clocks_event_reasons.active", _CSV),
        timeout=20,
    ),
    Operation(
        id="gpu.throttle_legacy",
        executable="nvidia-smi",
        args=("--query-gpu=index,clocks_throttle_reasons.active", _CSV),
        timeout=20,
    ),
    Operation(id="gpu.lspci_nvidia", executable="lspci", args=("-d", "10de:", "-nn")),
    # Kernel log. Both need root: without it journalctl can silently show only
    # the user's own messages, which would look like a clean log. journalctl
    # filters with --grep to keep output small; dmesg is filtered in Python.
    Operation(
        id="kernel.journal",
        executable="journalctl",
        args=(
            "-k",
            "-b",
            "--no-pager",
            "-o",
            "short-iso",
            "--case-sensitive=no",
            "-g",
            KERNEL_LOG_PATTERN,
        ),
        needs_root=True,
        timeout=30,
        max_output=1024 * 1024,
    ),
    Operation(
        id="kernel.dmesg",
        executable="dmesg",
        args=("-T",),
        needs_root=True,
        timeout=30,
        max_output=2 * 1024 * 1024,
    ),
    # The Vast daemon. `show` (not `is-active`) also reports LoadState, which is
    # the only way to tell a missing unit from a stopped one.
    Operation(
        id="vast.service_show",
        executable="systemctl",
        args=(
            "show",
            "vastai.service",
            "-p",
            "LoadState",
            "-p",
            "ActiveState",
            "-p",
            "SubState",
            "-p",
            "NRestarts",
            "--no-pager",
        ),
    ),
    # The log is root-only on a Vast host, so this is an elevated read: it runs
    # as root, or with elevated reads enabled, or reports "needs root".
    Operation(
        id="vast.daemon_log_tail",
        executable="tail",
        args=("-n", "100", "/var/lib/vastai_kaalia/kaalia.log"),
        needs_root=True,
    ),
    # Storage. Block-device operands must match a physical-drive pattern, and the
    # collector only passes names it got from lsblk.
    Operation(
        id="disk.df",
        executable="df",
        args=("-P", "-B1", "{path}"),
        operands={"path": re.compile(r"/[A-Za-z0-9_./+-]*")},
    ),
    Operation(
        id="disk.findmnt_root", executable="findmnt", args=("-no", "OPTIONS", "/")
    ),
    # -e 7 drops loop devices (the SSD health page warns about them).
    Operation(
        id="disk.lsblk",
        executable="lsblk",
        args=("-d", "-b", "-P", "-e", "7", "-o", "NAME,ROTA,SIZE,MODEL,TRAN"),
    ),
    # smartctl -x includes the overall-health line. No self-test option (-t) is
    # ever allowed: self-tests write and compete with client I/O.
    Operation(
        id="disk.smart_x",
        executable="smartctl",
        args=("-x", "{device}"),
        operands={"device": re.compile(r"/dev/(nvme\d+n\d+|sd[a-z]{1,3})")},
        needs_root=True,
        timeout=30,
    ),
    # Rental evidence: only each container's state, never names, ids, images,
    # environment, or logs. -a includes stopped containers (they can still belong
    # to a customer).
    Operation(
        id="rentals.docker_ps_all",
        executable="docker",
        args=("ps", "-a", "--format", "{{.State}}"),
        docker=True,
        timeout=20,
    ),
    # `docker=True`: the runner pins the trusted local socket and refuses
    # environments that point at another daemon.
    Operation(
        id="docker.info",
        executable="docker",
        args=("info", "--format", "{{json .}}"),
        docker=True,
        timeout=20,
    ),
    # Plain nvidia-smi: the header line carries the CUDA version.
    Operation(id="driver.smi_summary", executable="nvidia-smi", timeout=20),
)

READ_OPERATIONS: Mapping[str, Operation] = {op.id: op for op in _OPERATIONS}
