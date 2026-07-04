"""Validate mention / reviewer targets (SPECIFICATION §5.1).

The author may address any identifier; on submit we check each target's READ on the
anchor ``file_uid`` (as that target) and **error-mark** any that lack access. A
target that can't be resolved, or that lacks READ, is invalid — no mention/assignment
is persisted for it and the submit is rejected so the author can fix and resubmit.

Returns ``(valid, invalid)`` where ``valid`` is a list of ``(identifier, principal)``
(``principal.user`` is the canonical uid to store) and ``invalid`` is the list of
rejected identifiers.
"""
from __future__ import annotations

from typing import List, Tuple

from .ldap_auth import Identity


def validate_targets(directory, permissions, file_uid: str,
                     identifiers) -> Tuple[List[Tuple[str, Identity]], List[str]]:
    valid: List[Tuple[str, Identity]] = []
    invalid: List[str] = []
    seen: set[str] = set()
    for raw in (identifiers or []):
        key = (raw or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        principal = directory.resolve_principal(key)
        if principal is None or not permissions.can_read(principal, file_uid):
            invalid.append(key)
        else:
            valid.append((key, principal))
    return valid, invalid
