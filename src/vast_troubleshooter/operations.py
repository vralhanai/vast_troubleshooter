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
    # Plain nvidia-smi: the header line carries the CUDA version.
    Operation(id="driver.smi_summary", executable="nvidia-smi", timeout=20),
)

READ_OPERATIONS: Mapping[str, Operation] = {op.id: op for op in _OPERATIONS}
