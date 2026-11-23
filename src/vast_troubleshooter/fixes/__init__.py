"""Fixes: the only code allowed to change the system."""

from vast_troubleshooter.fixes.base import Fix, FixOutcome, FixStatus
from vast_troubleshooter.fixes.engine import FixEngine
from vast_troubleshooter.fixes.registry import FIXES, MUTATION_OPERATIONS
from vast_troubleshooter.fixes.storage import ActionLog, LockBusyError, ProcessLock

__all__ = [
    "FIXES",
    "MUTATION_OPERATIONS",
    "ActionLog",
    "Fix",
    "FixEngine",
    "FixOutcome",
    "FixStatus",
    "LockBusyError",
    "ProcessLock",
]
