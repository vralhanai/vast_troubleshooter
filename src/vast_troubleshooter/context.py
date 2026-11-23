"""What a collector is allowed to use to read the host."""

from dataclasses import dataclass

from vast_troubleshooter.files import FileSource, LocalFiles
from vast_troubleshooter.operations import READ_OPERATIONS
from vast_troubleshooter.runner import CommandRunner, SubprocessRunner


@dataclass(frozen=True, slots=True)
class HostContext:
    runner: CommandRunner
    files: FileSource


def default_context() -> HostContext:
    """The real host. Tests build a HostContext from fakes instead."""
    return HostContext(runner=SubprocessRunner(READ_OPERATIONS), files=LocalFiles())
