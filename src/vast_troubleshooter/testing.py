"""Test helpers. Collector tests use FakeRunner instead of real commands."""

from collections.abc import Mapping

from vast_troubleshooter.runner import CommandResult, CommandStatus


class FakeRunner:
    """Returns recorded results keyed by operation id. Never starts a process."""

    def __init__(self, results: Mapping[str, CommandResult | str]) -> None:
        self._results = dict(results)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def run(self, op_id: str, **operands: str) -> CommandResult:
        self.calls.append((op_id, dict(operands)))
        recorded = self._results.get(op_id)
        if recorded is None:
            return CommandResult(
                op_id=op_id,
                status=CommandStatus.REFUSED,
                detail="no fixture for this operation",
            )
        if isinstance(recorded, str):  # shorthand: stdout of a successful run
            return CommandResult(
                op_id=op_id, status=CommandStatus.OK, returncode=0, stdout=recorded
            )
        return recorded
