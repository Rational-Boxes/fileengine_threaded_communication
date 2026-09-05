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


def _tenant_role_base(cfg, tenant: str) -> str:
    """Where this tenant's groups live: ``ou=<tenant>,<tenant_base>``.

    Roles are per tenant and their CNs REPEAT across tenants — `administrators`,
    `engineering` and `accounting` all exist under more than one `ou=` on the
    deployment. Searching the whole tenant base therefore returns a union: a
    user who is `administrators` in one tenant looked like an administrator in
    every tenant. Scoping the base is what makes the answer mean "in THIS
    tenant"."""
    return f"ou={tenant},{cfg.ldap_tenant_base}"


def _roles_in_tenant(svc, cfg, user_dn: str, tenant: str) -> list[str]:
    """The user's groups within ``tenant``. Empty means NOT A MEMBER.

    That emptiness is the membership test used by both lookups below. There is
    no separate "is a member" record to consult — belonging to a tenant IS
    holding at least one group beneath its ou."""
    roles: list[str] = []
    try:
        svc.search(_tenant_role_base(cfg, tenant),
                   f"(&(objectClass=groupOfNames)(member={user_dn}))",
                   search_scope=SUBTREE, attributes=["cn"])
    except LDAPException:
        log.warning("directory: role lookup failed for %s in %s", user_dn, tenant, exc_info=True)
        return []
    for e in svc.entries:
        cn = str(e.cn)
        if cn and cn not in roles:
            roles.append(cn)
    if "administrators" in roles and "system_admin" not in roles:
        roles.append("system_admin")
    return roles


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

            scope = tenant or cfg.tenant
            roles = _roles_in_tenant(svc, cfg, user_dn, scope)
            if not roles:
                # Not a member of this tenant. Refused rather than returned
                # role-less: the caller's only other filter is an ACL check, and
                # the core is read-by-default, so a role-less outsider passes it
                # and could be mentioned into a document in a tenant they have
                # nothing to do with.
                return None

            return Identity(user=uid, roles=roles, tenant=scope,
                            authenticated=False, email=email)
        except LDAPException:
            log.warning("directory: lookup failed for %s", identifier, exc_info=True)
            return None
        finally:
            svc.unbind()

    def search(self, query: str, limit: int = 8,
               tenant: Optional[str] = None) -> list[Identity]:
        """Members of ``tenant`` matching ``query`` (uid/email/name substring),
        with their roles IN that tenant — for @mention autocomplete.

        MEMBERS ONLY. The LDAP user base is shared by every tenant, so the
        substring match alone returns strangers: on the deployment, a search for
        "a" on one tenant offered seven people, three of whom belonged to
        another tenant entirely. Their names and addresses were shown to users
        with no relationship to them.

        The ACL filter the caller applies afterwards does NOT cover this. The
        core is read-by-default, so a principal with no roles passes a READ check
        on any document without a matching deny rule — which is exactly what an
        outsider is. Membership has to be established here, and it is: no roles
        in this tenant, no place in the list.

        Returns [] on empty query or an unreachable directory."""
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
        scope = tenant or cfg.tenant
        out: list[Identity] = []
        try:
            # The directory is shared by every tenant, so this matches people the
            # caller must never be shown. _roles_in_tenant below is the filter
            # that makes the result "members of THIS tenant" — see the docstring.
            svc.search(cfg.ldap_user_base, f"(|(uid=*{q}*)(mail=*{q}*)(cn=*{q}*))",
                       search_scope=SUBTREE, attributes=["uid", "cn", "mail"], size_limit=limit * 4)
            for entry in svc.entries[:limit]:
                uid = str(entry.uid) if "uid" in entry else ""
                if not uid:
                    continue
                email = str(entry.mail) if "mail" in entry and entry.mail else ""
                roles = _roles_in_tenant(svc, cfg, entry.entry_dn, scope)
                if not roles:
                    continue     # not a member of this tenant — never offered
                out.append(Identity(user=uid, roles=roles, tenant=scope,
                                    authenticated=False, email=email))
        except LDAPException:
            log.warning("directory: search failed for %s", q, exc_info=True)
        finally:
            svc.unbind()
        return out
