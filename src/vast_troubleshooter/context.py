"""What a collector is allowed to use to read the host."""

from dataclasses import dataclass, field

from vast_troubleshooter.files import FileSource, LocalFiles
from vast_troubleshooter.netaccess import LocalNetwork, NetworkAccess, NoNetwork
from vast_troubleshooter.operations import READ_OPERATIONS
from vast_troubleshooter.runner import CommandRunner, SubprocessRunner


@dataclass(frozen=True, slots=True)
class HostContext:
    runner: CommandRunner
    files: FileSource
    network: NetworkAccess = field(default_factory=NoNetwork)
    # Outbound probes (connections to third-party servers) run only when true.
    allow_outbound: bool = False


def default_context(
    allow_outbound: bool = False, allow_elevated: bool = False
) -> HostContext:
    """The real host. Tests build a HostContext from fakes instead."""
    return HostContext(
        runner=SubprocessRunner(READ_OPERATIONS, allow_elevated=allow_elevated),
        files=LocalFiles(),
        network=LocalNetwork(),
        allow_outbound=allow_outbound,
    )
