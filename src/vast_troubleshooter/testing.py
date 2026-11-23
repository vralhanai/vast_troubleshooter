"""Test helpers. Collector tests use these instead of the real host."""

from collections.abc import Callable, Mapping

from vast_troubleshooter.context import HostContext
from vast_troubleshooter.netaccess import NetworkAccess
from vast_troubleshooter.runner import CommandResult, CommandStatus


class FakeRunner:
    """Returns recorded results keyed by operation id. Never starts a process."""

    def __init__(self, results: Mapping[str, CommandResult | str]) -> None:
        self._results = dict(results)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def run(self, op_id: str, **operands: str) -> CommandResult:
        self.calls.append((op_id, dict(operands)))
        # An operation with operands can be keyed per call, e.g.
        # "disk.smart_x[device=/dev/sda]"; the plain op id is the fallback.
        suffix = ",".join(f"{k}={v}" for k, v in sorted(operands.items()))
        recorded = self._results.get(f"{op_id}[{suffix}]") if suffix else None
        if recorded is None:
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


class FakeMutationRunner:
    """Stands in for the mutation runner. Records every call; changes nothing.

    `argv` maps an op id to the command it would run. `results` scripts what
    running it returns (default: success). `needs_root` ops report "needs root"
    at planning time. `on_run` lets a test change a pretend host when a command
    "runs", so the fix engine can observe the change.
    """

    def __init__(
        self,
        argv: Mapping[str, list[str]],
        results: Mapping[str, CommandResult] | None = None,
        needs_root: frozenset[str] = frozenset(),
        on_run: Callable[[str], None] | None = None,
    ) -> None:
        self._argv = dict(argv)
        self._results = dict(results or {})
        self._needs_root = needs_root
        self._on_run = on_run
        self.plan_calls: list[str] = []
        self.run_calls: list[str] = []
        self.run_operands: list[dict[str, str]] = []

    def plan(
        self, op_id: str, **operands: str
    ) -> tuple[list[str], CommandResult | None]:
        self.plan_calls.append(op_id)
        if op_id not in self._argv:
            return [], CommandResult(
                op_id, CommandStatus.REFUSED, detail="operation is not allowlisted"
            )
        if op_id in self._needs_root:
            return [], CommandResult(
                op_id,
                CommandStatus.NEEDS_ROOT,
                detail="needs root; elevated reads are not enabled",
            )
        return list(self._argv[op_id]), None

    def run(self, op_id: str, **operands: str) -> CommandResult:
        self.run_calls.append(op_id)
        self.run_operands.append(dict(operands))
        if self._on_run is not None:
            self._on_run(op_id)
        return self._results.get(
            op_id,
            CommandResult(
                op_id, CommandStatus.OK, argv=tuple(self._argv[op_id]), returncode=0
            ),
        )


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


class FakeNetwork:
    """Scripted DNS and TCP results. Never touches the network.

    `resolve_result` is a list of addresses or an exception to raise. `connect_results`
    maps (host, port) to None (success) or an exception to raise; an unlisted
    destination raises ConnectionRefusedError.
    """

    def __init__(
        self,
        resolve_result: list[str] | Exception | None = None,
        connect_results: Mapping[tuple[str, int], Exception | None] | None = None,
    ) -> None:
        self._resolve = ["203.0.113.7"] if resolve_result is None else resolve_result
        self._connect = dict(connect_results or {})
        self.resolve_calls: list[tuple[str, float]] = []
        self.connect_calls: list[tuple[str, int, float]] = []

    def resolve(self, host: str, timeout: float) -> list[str]:
        self.resolve_calls.append((host, timeout))
        if isinstance(self._resolve, Exception):
            raise self._resolve
        return list(self._resolve)

    def connect(self, host: str, port: int, timeout: float) -> None:
        self.connect_calls.append((host, port, timeout))
        outcome = self._connect.get((host, port), ConnectionRefusedError())
        if outcome is not None:
            raise outcome


def fake_context(
    results: Mapping[str, CommandResult | str] | None = None,
    files: Mapping[str, str | Exception] | None = None,
    network: NetworkAccess | None = None,
    allow_outbound: bool = False,
) -> HostContext:
    kwargs: dict[str, object] = {}
    if network is not None:
        kwargs["network"] = network
    return HostContext(
        runner=FakeRunner(results or {}),
        files=FakeFiles(files or {}),
        allow_outbound=allow_outbound,
        **kwargs,  # type: ignore[arg-type]
    )
