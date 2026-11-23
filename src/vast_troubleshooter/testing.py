"""Test helpers. Collector tests use these instead of the real host."""

from collections.abc import Mapping

from vast_troubleshooter.context import HostContext
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


class FakeFiles:
    """Serves file text by path. A value that is an exception is raised instead.

    A path that is not present raises FileNotFoundError, like a real missing file.
    """

    def __init__(self, files: Mapping[str, str | Exception]) -> None:
        self._files = dict(files)

    def read_text(self, path: str) -> str:
        if path not in self._files:
            raise FileNotFoundError(path)
        value = self._files[path]
        if isinstance(value, Exception):
            raise value
        return value


def fake_context(
    results: Mapping[str, CommandResult | str] | None = None,
    files: Mapping[str, str | Exception] | None = None,
) -> HostContext:
    return HostContext(runner=FakeRunner(results or {}), files=FakeFiles(files or {}))
