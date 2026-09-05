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

"""The directory offers MEMBERS of the asked-for tenant, and nobody else.

Against the real ``Directory`` with a stubbed LDAP connection, deliberately: the
API-level tests inject a fake directory, so they can only show that the endpoint
passes the tenant through — they cannot catch this, because the fake is where the
rule would be reimplemented. The rule lives here, so it is tested here.

The disclosure this pins, measured on the deployment: a search for "a" on one
tenant offered seven people, three of whom were members of a different tenant.
Their names and email addresses were shown to users with no relationship to
them. The ACL filter downstream does not cover it — the core is read-by-default,
so a role-less outsider passes a READ check on any document without a matching
deny rule, which is exactly what an outsider is.
"""
import pytest

from discussion import directory as directory_mod
from discussion.directory import Directory


class FakeEntry:
    def __init__(self, dn, uid=None, mail=None, cn=None):
        self.entry_dn = dn
        self._vals = {"uid": uid, "mail": mail, "cn": cn}

    def __contains__(self, key):
        return self._vals.get(key) is not None

    def __getattr__(self, key):
        if key.startswith("_"):
            raise AttributeError(key)
        return self._vals.get(key)


class FakeConn:
    """Answers a user search, then a per-tenant role search, recording the bases.

    ``groups`` maps a user DN to {tenant: [role cn, ...]} — the shape the real
    tree has, where a role only exists beneath its own ``ou=<tenant>``.
    """

    def __init__(self, users, groups):
        self.users, self.groups = users, groups
        self.entries = []
        self.bases = []          # every search base asked for, in order

    def search(self, base, filt, search_scope=None, attributes=None, size_limit=None):
        self.bases.append(base)
        if "member=" in filt:
            dn = filt.split("member=")[1].rstrip("))")
            # Only groups under THIS base are visible — the point of scoping.
            found = []
            for tenant, roles in self.groups.get(dn, {}).items():
                tenant_ou = f"ou={tenant},"
                if base.startswith(tenant_ou) or base == f"ou={tenant},ou=tenants,dc=x":
                    found += [FakeEntry(f"cn={r},ou={tenant},ou=tenants,dc=x", cn=r)
                              for r in roles]
            self.entries = found
            return True
        q = filt.split("uid=*")[1].split("*")[0] if "uid=*" in filt else \
            filt.split("uid=")[1].split(")")[0]
        self.entries = [e for e in self.users if q.lower() in (e.uid or "").lower()]
        return True

    def unbind(self):
        pass


class Cfg:
    ldap_uri = "ldap://x"
    ldap_bind_dn = "cn=svc"
    ldap_bind_password = "pw"
    ldap_user_base = "ou=users,dc=x"
    ldap_tenant_base = "ou=tenants,dc=x"
    tenant = "default"


INSIDER_DN = "uid=carol,ou=users,dc=x"
OUTSIDER_DN = "uid=carl,ou=users,dc=x"

USERS = [FakeEntry(INSIDER_DN, uid="carol", mail="carol@x"),
         FakeEntry(OUTSIDER_DN, uid="carl", mail="carl@x")]
GROUPS = {
    INSIDER_DN: {"arcdigital": ["users", "engineering"]},
    OUTSIDER_DN: {"default": ["users", "administrators"]},
}


@pytest.fixture
def conn(monkeypatch):
    c = FakeConn(USERS, GROUPS)
    monkeypatch.setattr(directory_mod, "Server", lambda *a, **k: object())
    monkeypatch.setattr(directory_mod, "Connection", lambda *a, **k: c)
    return c


def test_search_offers_members_and_omits_everyone_else(conn):
    # "car" matches both; only one belongs to arcdigital.
    got = Directory(Cfg()).search("car", 8, tenant="arcdigital")

    assert [i.user for i in got] == ["carol"]


def test_search_asks_only_this_tenants_branch_for_roles(conn):
    Directory(Cfg()).search("car", 8, tenant="arcdigital")

    role_bases = [b for b in conn.bases if b != Cfg.ldap_user_base]
    assert role_bases, "no role lookup happened"
    # Never the shared base — that is what merged roles across tenants.
    assert all(b == "ou=arcdigital,ou=tenants,dc=x" for b in role_bases), role_bases


def test_roles_are_the_ones_held_in_this_tenant(conn):
    got = Directory(Cfg()).search("carol", 8, tenant="arcdigital")

    assert sorted(got[0].roles) == ["engineering", "users"]
    # The outsider is 'administrators' in `default`. Were the search unscoped,
    # that would arrive here as administrators + system_admin in arcdigital.
    assert "administrators" not in got[0].roles
    assert "system_admin" not in got[0].roles


def test_resolve_principal_refuses_a_non_member(conn):
    d = Directory(Cfg())

    assert d.resolve_principal("carl", tenant="arcdigital") is None   # member of default
    assert d.resolve_principal("carol", tenant="arcdigital") is not None


def test_resolve_principal_still_serves_the_tenant_they_are_in(conn):
    got = Directory(Cfg()).resolve_principal("carl", tenant="default")

    assert got is not None and got.user == "carl"
    assert got.tenant == "default"
    assert "system_admin" in got.roles      # administrators there, and only there
