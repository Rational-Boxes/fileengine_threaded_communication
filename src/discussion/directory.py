"""Resolve a mention/reviewer *target* to a principal without authenticating them.

A mention or review can address any email/uid (§5.1). To enforce "you cannot flag
someone into a document they can't see", we must evaluate the *target's* READ on the
anchor — which needs the target's roles, not their password. Role membership is a
service-bind group lookup (no bind as the user), so we can resolve a principal
(uid + roles) from an identifier and hand it to Permissions.can_read.

Returns an Identity with ``authenticated=False`` (we did not authenticate them —
only resolved their identity for an ACL check). ``None`` if not found / unreachable.
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

    def resolve_principal(self, identifier: str) -> Optional[Identity]:
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

            return Identity(user=uid, roles=roles, tenant=cfg.tenant, authenticated=False, email=email)
        except LDAPException:
            log.warning("directory: lookup failed for %s", identifier, exc_info=True)
            return None
        finally:
            svc.unbind()

    def search(self, query: str, limit: int = 8) -> list[Identity]:
        """Candidate users matching ``query`` (uid/email/name substring), with roles
        resolved — for @mention autocomplete. The caller ACL-filters by the anchor
        (§5.1). Returns [] on empty query or an unreachable directory."""
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
                out.append(Identity(user=uid, roles=roles, tenant=cfg.tenant,
                                    authenticated=False, email=email))
        except LDAPException:
            log.warning("directory: search failed for %s", q, exc_info=True)
        finally:
            svc.unbind()
        return out
