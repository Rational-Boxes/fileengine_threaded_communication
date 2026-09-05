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

"""Resolve a mention/reviewer *target* to a principal without authenticating them.

A mention or review can address any email/uid (§5.1). To enforce "you cannot flag
someone into a document they can't see", we must evaluate the *target's* READ on the
anchor — which needs the target's roles, not their password. Role membership is a
service-bind group lookup (no bind as the user), so we can resolve a principal
(uid + roles) from an identifier and hand it to Permissions.can_read.

Returns an Identity with ``authenticated=False`` (we did not authenticate them —
only resolved their identity for an ACL check). ``None`` if not found / unreachable.

THE TENANT IS A PARAMETER, not the service's configured one. The resolved
principal is handed straight to ``Permissions.can_read``, which scopes its core
client by ``identity.tenant`` — so stamping the config's tenant asked the wrong
schema whenever the request was for any other tenant. The answer was False for
everyone, including the caller reading the file at that moment, and an empty
list is indistinguishable from "nobody matched": @mention autocomplete returned
200 with no users on every tenant but the configured one, and posting a mention
there failed 422 "cannot access this file". Measured on the deployment, where
the configured tenant is `default` and the users were in `arcdigital`.
"""
from __future__ import annotations

import logging
from typing import Optional

from ldap3 import Server, Connection, ALL, SUBTREE
from ldap3.core.exceptions import LDAPException

from .ldap_auth import Identity

log = logging.getLogger("discussion.directory")


class Directory:
    def __init__(self, config):
        self.config = config

    def resolve_principal(self, identifier: str,
                          tenant: Optional[str] = None) -> Optional[Identity]:
        """Resolve one identifier to a principal, scoped to ``tenant`` (default:
        the configured one) so the caller's ACL check runs in the right schema."""
        cfg = self.config
        identifier = (identifier or "").strip()
        if not identifier:
            return None
        try:
            server = Server(cfg.ldap_uri, get_info=ALL)
            svc = Connection(server, cfg.ldap_bind_dn, cfg.ldap_bind_password, auto_bind=True)
        except LDAPException:
            log.warning("directory: service bind failed", exc_info=True)
            return None
        try:
            # Address by uid OR email — the author may type either (§5.1).
            svc.search(cfg.ldap_user_base, f"(|(uid={identifier})(mail={identifier}))",
                       search_scope=SUBTREE, attributes=["uid", "cn", "mail"])
            if not svc.entries:
                return None
            entry = svc.entries[0]
            user_dn = entry.entry_dn
            uid = str(entry.uid) if "uid" in entry else identifier
            email = str(entry.mail) if "mail" in entry and entry.mail else (
                identifier if "@" in identifier else "")

            roles: list[str] = []
            svc.search(cfg.ldap_tenant_base,
                       f"(&(objectClass=groupOfNames)(member={user_dn}))",
                       search_scope=SUBTREE, attributes=["cn"])
            for e in svc.entries:
                cn = str(e.cn)
                if cn and cn not in roles:
                    roles.append(cn)
            if "administrators" in roles and "system_admin" not in roles:
                roles.append("system_admin")

            return Identity(user=uid, roles=roles, tenant=tenant or cfg.tenant,
                            authenticated=False, email=email)
        except LDAPException:
            log.warning("directory: lookup failed for %s", identifier, exc_info=True)
            return None
        finally:
            svc.unbind()

    def search(self, query: str, limit: int = 8,
               tenant: Optional[str] = None) -> list[Identity]:
        """Candidate users matching ``query`` (uid/email/name substring), with roles
        resolved — for @mention autocomplete. The caller ACL-filters by the anchor
        (§5.1), which is why ``tenant`` matters here: it is the tenant that filter
        runs in. Returns [] on empty query or an unreachable directory."""
        cfg = self.config
        q = (query or "").strip()
        if not q:
            return []
        try:
            server = Server(cfg.ldap_uri, get_info=ALL)
            svc = Connection(server, cfg.ldap_bind_dn, cfg.ldap_bind_password, auto_bind=True)
        except LDAPException:
            log.warning("directory: service bind failed", exc_info=True)
            return []
        out: list[Identity] = []
        try:
            svc.search(cfg.ldap_user_base, f"(|(uid=*{q}*)(mail=*{q}*)(cn=*{q}*))",
                       search_scope=SUBTREE, attributes=["uid", "cn", "mail"], size_limit=limit * 4)
            for entry in svc.entries[:limit]:
                uid = str(entry.uid) if "uid" in entry else ""
                if not uid:
                    continue
                email = str(entry.mail) if "mail" in entry and entry.mail else ""
                roles: list[str] = []
                svc.search(cfg.ldap_tenant_base,
                           f"(&(objectClass=groupOfNames)(member={entry.entry_dn}))",
                           search_scope=SUBTREE, attributes=["cn"])
                for e in svc.entries:
                    cn = str(e.cn)
                    if cn and cn not in roles:
                        roles.append(cn)
                if "administrators" in roles and "system_admin" not in roles:
                    roles.append("system_admin")
                out.append(Identity(user=uid, roles=roles, tenant=tenant or cfg.tenant,
                                    authenticated=False, email=email))
        except LDAPException:
            log.warning("directory: search failed for %s", q, exc_info=True)
        finally:
            svc.unbind()
        return out
