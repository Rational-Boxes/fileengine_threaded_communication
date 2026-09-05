# Copyright (C) 2026 James Hickman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

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
                     identifiers,
                     tenant: str = "") -> Tuple[List[Tuple[str, Identity]], List[str]]:
    """Resolve each identifier and keep those who can READ ``file_uid``.

    ``tenant`` is the tenant the check runs in — the request's, not the service's
    configured one. Without it every target resolved into the configured tenant's
    schema and failed the READ check there, so a mention or a review named
    anybody outside that tenant was rejected as "cannot access this file"."""
    valid: List[Tuple[str, Identity]] = []
    invalid: List[str] = []
    seen: set[str] = set()
    for raw in (identifiers or []):
        key = (raw or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        principal = directory.resolve_principal(key, tenant=tenant or None)
        if principal is None or not permissions.can_read(principal, file_uid):
            invalid.append(key)
        else:
            valid.append((key, principal))
    return valid, invalid
