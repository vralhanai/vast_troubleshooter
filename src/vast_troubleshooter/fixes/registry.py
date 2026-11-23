"""The fix registry and the mutation allowlist. Both are empty on purpose.

Step 14 builds the engine; no fix is wired up yet. Fixes arrive in step 15, each
one reviewed. The mutation allowlist is separate from the read allowlist in
`operations.py`: the read runner can never run anything registered here, and
this runner can never run anything registered there.
"""

from collections.abc import Mapping

from vast_troubleshooter.fixes.base import Fix
from vast_troubleshooter.runner import Operation

FIXES: Mapping[str, Fix] = {}
MUTATION_OPERATIONS: Mapping[str, Operation] = {}
