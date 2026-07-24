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

"""Real-time comment sync + co-viewing presence (SPECIFICATION §10h).

The one bounded real-time exception: while users have the *same file's* comment panel
open, comments sync live between them and a small "also here" roster shows co-viewers.
Confined to an open panel — no global surface, no typing indicators.

``LiveHub`` is the in-process registry of open ``WS /files/{uid}/live`` connections
keyed by ``(tenant, file_uid)``. It fans a message out to a file's subscribers and
maintains the presence roster. Cross-replica fan-out (Redis pub/sub) is layered on
top via ``bridge_publish``/an external listener that calls ``deliver_local`` — kept
separate so the routing/roster logic here is pure and unit-testable.

ACL is re-checked per push (§10h) via the cached ``Permissions.can_read`` — cheap
after the join-time check, and evicted on ``acl.changed`` (M4a) — so a mid-session
revoke stops delivery. Redactions arrive as masked events (the handler passes the
masked comment), never re-broadcasting the original.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Optional

log = logging.getLogger("discussion.live")


class Connection:
    """One open panel socket. ``invisible`` (admin-only, §10h) omits it from the
    roster but not from delivery — it still sees others and receives comments."""
    def __init__(self, ws, identity, file_uid: str, invisible: bool = False):
        self.ws = ws
        self.identity = identity
        self.file_uid = file_uid
        self.invisible = invisible

    @property
    def user(self) -> str:
        return self.identity.user

    @property
    def tenant(self) -> str:
        return self.identity.tenant

    async def send(self, message: dict) -> None:
        await self.ws.send_json(message)


class LiveHub:
    def __init__(self, config, permissions, *, bridge=None):
        self.config = config
        self.perms = permissions
        self.bridge = bridge          # optional cross-replica publisher (Redis pub/sub)
        # Insertion-ordered set of connections per file (dict keys) so the presence
        # roster is deterministic (join order), not hash-order.
        self._subs: dict[tuple[str, str], dict[Connection, None]] = defaultdict(dict)
        self._count = 0

    @staticmethod
    def _key(tenant: str, file_uid: str):
        return (tenant, file_uid)

    def total(self) -> int:
        return self._count

    def roster(self, tenant: str, file_uid: str) -> list[str]:
        """Distinct *visible* co-viewers of this file (invisible admins excluded)."""
        seen: list[str] = []
        for c in self._subs.get(self._key(tenant, file_uid), ()):
            if not c.invisible and c.user not in seen:
                seen.append(c.user)
        return seen

    async def join(self, conn: Connection) -> None:
        self._subs[self._key(conn.tenant, conn.file_uid)][conn] = None
        self._count += 1
        await self._send_presence(conn.tenant, conn.file_uid)

    async def leave(self, conn: Connection) -> None:
        key = self._key(conn.tenant, conn.file_uid)
        subs = self._subs.get(key)
        if not subs or conn not in subs:
            return
        subs.pop(conn, None)
        self._count -= 1
        if not subs:
            self._subs.pop(key, None)
        await self._send_presence(conn.tenant, conn.file_uid)

    async def broadcast(self, tenant: str, file_uid: str, message: dict,
                        *, _local_only: bool = False) -> None:
        """Deliver ``message`` to this file's local subscribers (ACL re-checked per
        push) and, unless local-only, hand it to the cross-replica bridge."""
        await self.deliver_local(tenant, file_uid, message)
        if not _local_only and self.bridge is not None:
            try:
                self.bridge.publish(tenant, file_uid, message)
            except Exception:
                log.warning("live bridge publish failed", exc_info=True)

    async def deliver_local(self, tenant: str, file_uid: str, message: dict) -> None:
        for c in list(self._subs.get(self._key(tenant, file_uid), ())):
            try:
                if not self.perms.can_read(c.identity, file_uid):
                    continue                          # mid-session revoke → stop delivery
            except Exception:
                continue
            try:
                await c.send(message)
            except Exception:
                pass

    async def _send_presence(self, tenant: str, file_uid: str) -> None:
        viewers = self.roster(tenant, file_uid)
        msg = {"type": "presence", "viewers": viewers, "count": len(viewers)}
        for c in list(self._subs.get(self._key(tenant, file_uid), ())):
            try:
                await c.send(msg)
            except Exception:
                pass
