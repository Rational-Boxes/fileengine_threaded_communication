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

"""Per-tenant Postgres schema isolation — mirrors the core's tenant↔schema model.

Each tenant's discussion data lives in its own ``tenant_<tenant>`` schema (empty →
``tenant_default``; ``-``/``.``/space etc. sanitized to ``_``). The schema *is* the
tenant, so tables carry no tenant column — scoping is by ``search_path``.

Database-wide objects (the ``vector`` / ``pg_trgm`` extensions) live once at the
database level (see ``migrations/0001_baseline.sql``). The per-tenant tables are
provisioned on demand by ``ensure_tenant_schema``. This DDL is the SPECIFICATION §4
data model; it is idempotent (``IF NOT EXISTS``).
"""
import re

_UNSAFE = re.compile(r"[^A-Za-z0-9_]")


def schema_name(tenant: str) -> str:
    """The tenant's schema: ``tenant_<sanitized-tenant>`` (empty → ``tenant_default``)."""
    t = (tenant or "").strip()
    if not t:
        return "tenant_default"
    return "tenant_" + _UNSAFE.sub("_", t)


# Idempotent DDL for one tenant's tables (SPECIFICATION §4), parameterized by schema
# name + pgvector dimension. V2 anchoring (§5.4 of the xeokit upgrade/BCF plan) has
# landed: `threads.anchor` is the nullable JSONB viewpoint/region anchor (NULL = a
# plain file-level comment, unchanged) and `comments.viewpoint_ref` pins a comment to
# one of a topic's viewpoints (BCF semantics). Both are additive & nullable — no
# migration of existing rows.
_TENANT_DDL = '''
CREATE SCHEMA IF NOT EXISTS "{schema}";

-- A thread is pinned to an anchor. file_uid is the ACL authority.
CREATE TABLE IF NOT EXISTS "{schema}".threads (
    id               TEXT PRIMARY KEY,
    file_uid         TEXT NOT NULL,
    version          TEXT NOT NULL DEFAULT '',
    title            TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved')),
    resolved_by      TEXT,
    resolved_version TEXT,
    opened_by        TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    anchor_stale     BOOLEAN NOT NULL DEFAULT false,
    anchor           JSONB                            -- V2 (§5.4): discriminated-union viewpoint/region anchor; NULL = plain file-level comment
);
CREATE INDEX IF NOT EXISTS idx_threads_file ON "{schema}".threads (file_uid, status);
-- Self-heal tenants provisioned before the V2 anchor column landed (§5.4).
ALTER TABLE "{schema}".threads ADD COLUMN IF NOT EXISTS anchor JSONB;

CREATE TABLE IF NOT EXISTS "{schema}".comments (
    id                TEXT PRIMARY KEY,
    thread_id         TEXT NOT NULL REFERENCES "{schema}".threads (id) ON DELETE CASCADE,
    parent_comment_id TEXT REFERENCES "{schema}".comments (id) ON DELETE CASCADE,  -- nested replies
    author            TEXT NOT NULL,
    body              TEXT NOT NULL,                  -- Markdown, constrained subset (§4a)
    body_text         TEXT NOT NULL DEFAULT '',       -- plaintext projection for FTS + embeddings
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    edited_at         TIMESTAMPTZ,
    deleted           BOOLEAN NOT NULL DEFAULT false, -- author soft-delete
    redacted          BOOLEAN NOT NULL DEFAULT false, -- admin moderation (§5b)
    redacted_by       TEXT,
    redacted_at       TIMESTAMPTZ,
    redacted_reason   TEXT,
    viewpoint_ref     TEXT,                           -- V2 (§5.4): pin this comment to a topic viewpoint (BCF); NULL = unpinned
    fts               tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(body_text,''))) STORED
);
-- Self-heal existing tenants provisioned before nested replies / the V2 viewpoint pin.
ALTER TABLE "{schema}".comments ADD COLUMN IF NOT EXISTS parent_comment_id TEXT;
ALTER TABLE "{schema}".comments ADD COLUMN IF NOT EXISTS viewpoint_ref TEXT;
CREATE INDEX IF NOT EXISTS idx_comments_thread ON "{schema}".comments (thread_id, created_at);
CREATE INDEX IF NOT EXISTS idx_comments_parent ON "{schema}".comments (parent_comment_id);
CREATE INDEX IF NOT EXISTS idx_comments_fts ON "{schema}".comments USING gin (fts);

CREATE TABLE IF NOT EXISTS "{schema}".comment_revisions (
    comment_id TEXT NOT NULL REFERENCES "{schema}".comments (id) ON DELETE CASCADE,
    body       TEXT NOT NULL,
    edited_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Protected store of pre-redaction content (§5b). Retained forever.
CREATE TABLE IF NOT EXISTS "{schema}".redactions (
    comment_id    TEXT NOT NULL REFERENCES "{schema}".comments (id) ON DELETE CASCADE,
    original_body TEXT NOT NULL,
    redacted_by   TEXT NOT NULL,
    reason        TEXT NOT NULL DEFAULT '',
    redacted_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS "{schema}".mentions (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    comment_id  TEXT NOT NULL REFERENCES "{schema}".comments (id) ON DELETE CASCADE,
    thread_id   TEXT NOT NULL,
    target_user TEXT NOT NULL,                      -- MUST hold READ on the thread's file_uid (§5.1)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_mentions_target ON "{schema}".mentions (target_user, created_at DESC);

CREATE TABLE IF NOT EXISTS "{schema}".review_requests (
    id              TEXT PRIMARY KEY,
    file_uid        TEXT NOT NULL,
    version         TEXT NOT NULL DEFAULT '',
    thread_id       TEXT,
    requester       TEXT NOT NULL,
    reviewer        TEXT NOT NULL,                  -- one row per reviewer
    status          TEXT NOT NULL DEFAULT 'requested'
                    CHECK (status IN ('requested','acknowledged','completed','declined')),
    outcome         TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    acknowledged_at TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_reviews_reviewer ON "{schema}".review_requests (reviewer, status);
CREATE INDEX IF NOT EXISTS idx_reviews_requester ON "{schema}".review_requests (requester, status);

-- The attention feed backing store (one row per thing wanting a user's attention).
CREATE TABLE IF NOT EXISTS "{schema}".notifications (
    id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id   TEXT NOT NULL,
    kind      TEXT NOT NULL CHECK (kind IN
                ('mention','reply','review_requested','review_acknowledged',
                 'review_completed','thread_resolved')),
    file_uid  TEXT NOT NULL,                        -- for a read-time ACL re-check before display
    thread_id TEXT,
    review_id TEXT,
    actor     TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_notif_user ON "{schema}".notifications (user_id, read_at, created_at DESC);

-- Comment text vectorized for RAG (§6). Keyed by anchor file_uid so the existing
-- can_read(file_uid) gate applies unchanged.
CREATE TABLE IF NOT EXISTS "{schema}".comment_chunks (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    comment_id TEXT NOT NULL REFERENCES "{schema}".comments (id) ON DELETE CASCADE,
    file_uid   TEXT NOT NULL,
    thread_id  TEXT NOT NULL,
    text       TEXT NOT NULL,
    embedding  vector({dimension}),
    fts        tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
);
CREATE INDEX IF NOT EXISTS idx_comment_chunks_hnsw
    ON "{schema}".comment_chunks USING hnsw (embedding vector_cosine_ops);

-- Durable projection of core file events (§8) for the dashboard activity feed (§10a)
-- and the email digest (§11).
CREATE TABLE IF NOT EXISTS "{schema}".document_activity (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    file_uid   TEXT NOT NULL,
    event_type TEXT NOT NULL,                       -- 'created' | 'updated' | 'restored'
    version    TEXT NOT NULL DEFAULT '',
    name       TEXT NOT NULL DEFAULT '',
    path       TEXT NOT NULL DEFAULT '',
    actor      TEXT NOT NULL DEFAULT '',
    ts         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON "{schema}".document_activity (ts DESC);
CREATE INDEX IF NOT EXISTS idx_activity_file ON "{schema}".document_activity (file_uid);

-- Per-user email-digest subscription (§11a).
CREATE TABLE IF NOT EXISTS "{schema}".digest_subscriptions (
    user_id         TEXT PRIMARY KEY,
    cadence         TEXT NOT NULL DEFAULT 'off'
                    CHECK (cadence IN ('off','hourly','daily','weekly')),
    send_hour_local SMALLINT NOT NULL DEFAULT 8   CHECK (send_hour_local BETWEEN 0 AND 23),
    send_dow        SMALLINT NOT NULL DEFAULT 1   CHECK (send_dow BETWEEN 0 AND 6),
    timezone        TEXT NOT NULL DEFAULT 'UTC',
    scope           JSONB NOT NULL DEFAULT '{{}}',
    ai_summary      BOOLEAN NOT NULL DEFAULT false,
    quiet_if_empty  BOOLEAN NOT NULL DEFAULT true,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per (user, period) the digest sender processed — the idempotency guard (§11c).
CREATE TABLE IF NOT EXISTS "{schema}".digest_deliveries (
    user_id    TEXT NOT NULL,
    period_key TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('sent','skipped_empty','error')),
    item_count INTEGER NOT NULL DEFAULT 0,
    error      TEXT,
    sent_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, period_key)
);
'''


def tenant_ddl(tenant: str, dimension: int = 1024) -> str:
    """The idempotent DDL that provisions a tenant's schema + tables.

    ``dimension`` is the pgvector embedding width — must match the deployment's
    DISC_EMBEDDING_DIMENSION / the chosen embedding model."""
    return _TENANT_DDL.format(schema=schema_name(tenant), dimension=int(dimension))


def ensure_tenant_schema(conn, tenant: str, dimension: int = 1024) -> str:
    """Create the tenant's schema + tables if absent (idempotent). ``conn`` is an
    open psycopg connection (extensions must already exist at the DB level)."""
    name = schema_name(tenant)
    with conn.cursor() as cur:
        cur.execute(tenant_ddl(tenant, dimension))
    conn.commit()
    return name
