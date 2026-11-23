"""OS, kernel, and uptime."""

from typing import ClassVar

from vast_troubleshooter.collectors.base import Collector
from vast_troubleshooter.context import HostContext
from vast_troubleshooter.models import Fact, FactStatus

OS_RELEASE = "/etc/os-release"
PROC_UPTIME = "/proc/uptime"


def parse_os_release(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


class SystemCollector(Collector):
    name: ClassVar[str] = "system"

    def collect(self, ctx: HostContext) -> list[Fact]:
        return [*self._os(ctx), self._kernel(ctx), self._uptime(ctx)]

    def _read(self, ctx: HostContext, path: str) -> tuple[str | None, Fact | None]:
        """Read an allowlisted file. Returns (text, None) or (None, failure fact)."""
        key = f"system.{'os_release' if path == OS_RELEASE else 'uptime'}"
        try:
            return ctx.files.read_text(path), None
        except FileNotFoundError:
            return None, self.fact(
                key, status=FactStatus.MISSING, source_command=f"read {path}"
            )
        except OSError as exc:
            return None, self.fact(
                key,
                status=FactStatus.ERROR,
                source_command=f"read {path}",
                raw_excerpt=f"{type(exc).__name__}: {exc}",
            )

    def _os(self, ctx: HostContext) -> list[Fact]:
        text, failure = self._read(ctx, OS_RELEASE)
        if failure is not None:
            return [failure]
        assert text is not None
        values = parse_os_release(text)
        source = f"read {OS_RELEASE}"
        facts = []
        for key, field in (
            ("system.os_id", "ID"),
            ("system.os_version_id", "VERSION_ID"),
            ("system.os_pretty_name", "PRETTY_NAME"),
        ):
            if field in values:
                facts.append(self.fact(key, values[field], source_command=source))
            else:
                facts.append(
                    self.fact(key, status=FactStatus.MISSING, source_command=source)
                )
        return facts

    def _kernel(self, ctx: HostContext) -> Fact:
        result = ctx.runner.run("system.uname_release")
        key = "system.kernel_release"
        if not result.ok:
            return self.failed_from_result(key, result)
        release = result.stdout.strip()
        if not release:
            return self.fact(
                key,
                status=FactStatus.ERROR,
                source_command=result.source_command,
                raw_excerpt="uname -r printed nothing",
            )
        return self.fact(key, release, source_command=result.source_command)

    def _uptime(self, ctx: HostContext) -> Fact:
        text, failure = self._read(ctx, PROC_UPTIME)
        if failure is not None:
            return failure
        assert text is not None
        source = f"read {PROC_UPTIME}"
        try:
            seconds = float(text.split()[0])
        except IndexError, ValueError:
            return self.fact(
                "system.uptime",
                status=FactStatus.ERROR,
                source_command=source,
                raw_excerpt=f"unparseable: {text[:100]!r}",
            )
        return self.fact(
            "system.uptime_seconds", seconds, unit="s", source_command=source
        )
