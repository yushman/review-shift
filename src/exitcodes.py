"""The exit-code contract, normatively defined once by ADR-010 and treated as CLI surface by
ADR-020: 0 ok, 1 findings (signal, not error), 2 internal error, 3 lock held, 4 auth/quota.
"""
from __future__ import annotations

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_INTERNAL_ERROR = 2
EXIT_LOCK_HELD = 3
EXIT_AUTH_OR_QUOTA = 4
