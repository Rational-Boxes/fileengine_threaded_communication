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

"""Per-tenant schema naming + DDL generation — hermetic (no DB)."""
import pytest

from discussion.schema import schema_name, tenant_ddl


@pytest.mark.parametrize("tenant,expected", [
    ("", "tenant_default"),
    (None, "tenant_default"),
    ("default", "tenant_default"),
    ("acme", "tenant_acme"),
    ("a-b.c d", "tenant_a_b_c_d"),
])
def test_schema_name(tenant, expected):
    assert schema_name(tenant) == expected


def test_tenant_ddl_covers_the_spec_tables():
    ddl = tenant_ddl("acme", dimension=768)
    assert 'CREATE SCHEMA IF NOT EXISTS "tenant_acme"' in ddl
    # The embedding column width is the passed dimension.
    assert "vector(768)" in ddl
    # Every §4 table is present and schema-qualified.
    for table in ("threads", "comments", "comment_revisions", "redactions", "mentions",
                  "review_requests", "notifications", "comment_chunks", "document_activity",
                  "digest_subscriptions", "digest_deliveries"):
        assert f'"tenant_acme".{table}' in ddl, table


def test_tenant_ddl_is_idempotent_form():
    ddl = tenant_ddl("acme")
    # No bare CREATE TABLE / CREATE INDEX without IF NOT EXISTS.
    assert "CREATE TABLE " not in ddl.replace("CREATE TABLE IF NOT EXISTS ", "")
    assert "CREATE INDEX " not in ddl.replace("CREATE INDEX IF NOT EXISTS ", "")


def test_jsonb_default_literal_intact():
    # The doubled brace in the template must render to a real empty-json default.
    assert "DEFAULT '{}'" in tenant_ddl("acme")


def test_tenant_ddl_has_v2_anchor_and_viewpoint_columns():
    """V2 (§5.4): threads.anchor JSONB + comments.viewpoint_ref, with self-heal ALTERs."""
    ddl = tenant_ddl("acme", dimension=768)
    assert "anchor           JSONB" in ddl or "anchor JSONB" in ddl
    assert "viewpoint_ref" in ddl
    # Additive self-heal for pre-existing tenants (idempotent form).
    assert 'ALTER TABLE "tenant_acme".threads ADD COLUMN IF NOT EXISTS anchor JSONB' in ddl
    assert 'ALTER TABLE "tenant_acme".comments ADD COLUMN IF NOT EXISTS viewpoint_ref TEXT' in ddl
