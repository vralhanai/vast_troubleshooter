"""The rental state as facts, so `diagnose` can show it.

Counts and the state only: never container names, ids, images, or logs.
"""

from typing import ClassVar

from vast_troubleshooter.collectors.base import Collector
from vast_troubleshooter.context import HostContext
from vast_troubleshooter.models import Fact
from vast_troubleshooter.rentals import check_rentals


class RentalsCollector(Collector):
    name: ClassVar[str] = "rentals"

    def collect(self, ctx: HostContext) -> list[Fact]:
        assessment = check_rentals(ctx)
        facts = [
            # `unknown` is a legitimate value here, not a failed read.
            self.fact("rentals.state", assessment.state.value),
            self.fact("rentals.blocks_disruptive_fixes", assessment.blocks_disruption),
        ]
        if assessment.reason:
            facts.append(self.fact("rentals.reason", assessment.reason[:190]))
        unresolved = assessment.unresolved
        if unresolved:
            facts.append(self.fact("rentals.unresolved_sources", ",".join(unresolved)))
        for reading in assessment.readings:
            for key, value in reading.detail.items():
                facts.append(self.fact(f"rentals.{reading.source}_{key}", value))
        return facts
