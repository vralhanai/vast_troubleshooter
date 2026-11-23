"""The registry of read-only operations collectors may run.

Add an operation here only with a collector that needs it. Nothing in this
registry may change the system.
"""

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
