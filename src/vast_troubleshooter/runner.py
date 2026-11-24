"""Allowlisted, timeout-bounded command runner.

Every collector reads the system through this module. Callers never pass a
command line. They name an *operation* from a fixed registry, plus validated
operands. The runner then:

* never uses a shell (argument lists only),
* runs with a minimal environment (no inherited secrets or overrides),
* kills the whole process group on timeout and bounds captured output,
* refuses Docker when the environment points at another daemon,
* reports "needs root" instead of escalating unless elevated reads are allowed.

There is no mutation allowlist yet. It arrives with the fix engine (step 14).
"""

import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import IO, Protocol

TRUSTED_DOCKER_ENDPOINT = "unix:///var/run/docker.sock"
DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_OUTPUT = 256 * 1024

_POSIX = os.name == "posix"
_SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


class CommandStatus(StrEnum):
    OK = "ok"
    MISSING = "missing"  # executable not found
    ERROR = "error"  # started but failed (non-zero exit or could not start)
    TIMEOUT = "timeout"
    REFUSED = "refused"  # not allowed by policy; nothing was run
    NEEDS_ROOT = "needs_root"  # needs elevation that is not enabled; nothing was run


@dataclass(frozen=True, slots=True)
class CommandResult:
    op_id: str
    status: CommandStatus
    argv: tuple[str, ...] = ()
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    duration: float = 0.0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is CommandStatus.OK

    @property
    def source_command(self) -> str:
        return shlex.join(self.argv)


@dataclass(frozen=True, slots=True)
class Operation:
    """One fixed, read-only way to run one program."""

    id: str
    executable: str
    args: tuple[str, ...] = ()
    # Dynamic operands. An argument equal to "{name}" is replaced by the
    # operand value, which must fully match the pattern.
    operands: Mapping[str, re.Pattern[str]] = field(default_factory=dict)
    needs_root: bool = False
    timeout: float = DEFAULT_TIMEOUT
    max_output: int = DEFAULT_MAX_OUTPUT
    docker: bool = False

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("operation id is empty")
        if not self.executable:
            raise ValueError(f"{self.id}: executable is empty")
        if not os.path.isabs(self.executable) and any(
            sep in self.executable for sep in "/\\"
        ):
            raise ValueError(
                f"{self.id}: executable must be a bare name or an absolute path"
            )
        for arg in self.args:
            match = _PLACEHOLDER.fullmatch(arg)
            if match and match.group(1) not in self.operands:
                raise ValueError(f"{self.id}: placeholder {arg} has no operand")


class CommandRunner(Protocol):
    def run(self, op_id: str, **operands: str) -> CommandResult: ...


def _default_which(name: str, path: str | None) -> str | None:
    # shutil.which takes `mode` second, so `path` must be passed by keyword.
    return shutil.which(name, path=path)


def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


# Seconds to wait for a killed command to exit before giving up on it.
_KILL_GRACE = 5.0


class _Capture:
    """Drain a pipe on a thread, keeping at most `limit` bytes."""

    def __init__(self, stream: IO[bytes], limit: int) -> None:
        self._stream = stream
        self._limit = limit
        self._buffer = bytearray()
        self.truncated = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while chunk := self._stream.read(8192):
                room = self._limit - len(self._buffer)
                if room > 0:
                    self._buffer += chunk[:room]
                if len(chunk) > room:
                    self.truncated = True
        except OSError, ValueError:
            pass

    def finish(self, wait: float = 2.0) -> str:
        self._thread.join(wait)
        return bytes(self._buffer).decode("utf-8", errors="replace")


class SubprocessRunner:
    def __init__(
        self,
        operations: Mapping[str, Operation],
        *,
        allow_elevated: bool = False,
        environ: Mapping[str, str] | None = None,
        is_root: Callable[[], bool] = _is_root,
        which: Callable[[str, str | None], str | None] = _default_which,
    ) -> None:
        self._operations = dict(operations)
        self._allow_elevated = allow_elevated
        self._environ = dict(os.environ if environ is None else environ)
        self._is_root = is_root
        self._which = which

    # -- planning (pure, no process is started) -------------------------------

    def plan(
        self, op_id: str, **operands: str
    ) -> tuple[list[str], CommandResult | None]:
        """Return the argv to run, or a result explaining why nothing runs."""
        op = self._operations.get(op_id)
        if op is None:
            return [], _early(
                op_id, CommandStatus.REFUSED, "operation is not allowlisted"
            )

        problem = _operand_problem(op, operands)
        if problem:
            return [], _early(op_id, CommandStatus.REFUSED, problem)

        if op.docker:
            problem = self._docker_problem()
            if problem:
                return [], _early(op_id, CommandStatus.REFUSED, problem)

        search_path = _SAFE_PATH if _POSIX else None
        executable = op.executable
        if not os.path.isabs(executable):
            executable = self._which(executable, search_path) or ""
        if not executable:
            return [], _early(
                op_id, CommandStatus.MISSING, f"{op.executable} was not found"
            )

        argv = [executable]
        if op.docker:
            argv += ["--host", TRUSTED_DOCKER_ENDPOINT]
        for arg in op.args:
            match = _PLACEHOLDER.fullmatch(arg)
            argv.append(operands[match.group(1)] if match else arg)

        if op.needs_root and not self._is_root():
            if not self._allow_elevated:
                return [], _early(
                    op_id,
                    CommandStatus.NEEDS_ROOT,
                    "needs root; elevated reads are not enabled",
                    argv,
                )
            sudo = self._which("sudo", search_path)
            if not sudo:
                return [], _early(
                    op_id, CommandStatus.NEEDS_ROOT, "needs root; sudo not found", argv
                )
            argv = [sudo, "-n", "--", *argv]
        return argv, None

    def _docker_problem(self) -> str | None:
        host = self._environ.get("DOCKER_HOST", "")
        if host and host != TRUSTED_DOCKER_ENDPOINT:
            return "DOCKER_HOST points away from the trusted local endpoint"
        context = self._environ.get("DOCKER_CONTEXT", "")
        if context and context != "default":
            return "DOCKER_CONTEXT selects a non-default Docker context"
        return None

    # -- execution ------------------------------------------------------------

    def run(self, op_id: str, **operands: str) -> CommandResult:
        argv, early = self.plan(op_id, **operands)
        if early is not None:
            return early
        op = self._operations[op_id]
        return self._execute(op, argv)

    def _execute(self, op: Operation, argv: list[str]) -> CommandResult:
        start = time.monotonic()
        try:
            proc = subprocess.Popen(
                argv,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_child_env(),
                start_new_session=_POSIX,
            )
        except FileNotFoundError:
            return _early(op.id, CommandStatus.MISSING, "executable not found", argv)
        except OSError as exc:
            return _early(op.id, CommandStatus.ERROR, f"could not start: {exc}", argv)

        assert proc.stdout is not None and proc.stderr is not None
        out = _Capture(proc.stdout, op.max_output)
        err = _Capture(proc.stderr, op.max_output)
        timed_out = False
        still_running = False
        try:
            proc.wait(timeout=op.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            try:
                proc.wait(timeout=_KILL_GRACE)
            except subprocess.TimeoutExpired:
                # The kill was refused (for example a process running as root
                # under sudo). Never wait for it forever: report it clearly.
                still_running = True

        drain = 0.2 if still_running else 2.0  # its pipes stay open while it runs
        stdout, stderr = out.finish(drain), err.finish(drain)
        duration = time.monotonic() - start
        truncated = out.truncated or err.truncated
        base = {
            "op_id": op.id,
            "argv": tuple(argv),
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": truncated,
            "duration": duration,
        }
        if timed_out:
            note = (
                "; it could not be stopped and may still be running"
                if still_running
                else ""
            )
            return CommandResult(
                status=CommandStatus.TIMEOUT,
                detail=f"timed out after {op.timeout}s{note}",
                **base,  # type: ignore[arg-type]
            )
        if proc.returncode != 0:
            return CommandResult(
                status=CommandStatus.ERROR,
                detail=f"exit code {proc.returncode}",
                **base,  # type: ignore[arg-type]
            )
        return CommandResult(status=CommandStatus.OK, **base)  # type: ignore[arg-type]


def _early(
    op_id: str,
    status: CommandStatus,
    detail: str,
    argv: list[str] | None = None,
) -> CommandResult:
    return CommandResult(
        op_id=op_id, status=status, argv=tuple(argv or ()), detail=detail
    )


def _operand_problem(op: Operation, operands: Mapping[str, str]) -> str | None:
    if set(operands) != set(op.operands):
        return "operands do not match the operation definition"
    for name, value in operands.items():
        if not isinstance(value, str) or not value or "\x00" in value:
            return f"operand {name} is not a valid string"
        if value.startswith("-"):
            return f"operand {name} looks like an option"
        if not op.operands[name].fullmatch(value):
            return f"operand {name} does not match the allowed pattern"
    return None


def _child_env() -> dict[str, str]:
    env = {
        "LC_ALL": "C",
        "LANG": "C",
        "PAGER": "cat",
        "SYSTEMD_PAGER": "",
        "SYSTEMD_COLORS": "0",
    }
    if _POSIX:
        env["PATH"] = _SAFE_PATH
    else:  # Windows is for development only; it needs these to start programs.
        for name in ("PATH", "SYSTEMROOT"):
            if name in os.environ:
                env[name] = os.environ[name]
    return env


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    try:
        if _POSIX:
            os.killpg(proc.pid, signal.SIGKILL)  # type: ignore[attr-defined,unused-ignore]
        else:
            proc.kill()
    except ProcessLookupError, PermissionError, OSError:
        pass
