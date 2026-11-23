"""The registry of read-only operations collectors may run.

Add an operation here only with a collector that needs it. Nothing in this
registry may change the system.
"""

from collections.abc import Mapping

from vast_troubleshooter.runner import Operation

_OPERATIONS = (Operation(id="system.uname_release", executable="uname", args=("-r",)),)

READ_OPERATIONS: Mapping[str, Operation] = {op.id: op for op in _OPERATIONS}
