# Discussion & Threaded Communication — Service Specification

**Roadmap phase:** This service is the implementation of **Phase 2 — Anchored discussion
threads** in `scripts/documnets/FILEENGINE_ROADMAP.md`, with forward hooks into
**Phase 1 (provenance)** and **Phase 3 (ACL-filtered digests)**. Read that roadmap first;
this document is the engineering spec for the phase, not a restatement of its strategy.

> **The one-line thesis (roadmap Principle 6):** the discussion about the Q3 deck lives
> *on* the Q3 deck, forever, at the right permission level, and the AI can cite it. This is
> PR-review-comment semantics, not chat. We close the conversation→canon loop **without**
> building a chat platform.

A FastAPI backend for document-anchored threaded discussion and review requests. It follows
the integration patterns already established by **CSAI** (`convert_search_ai`) and the
**LDAP administration service** (`ldap_manager`): a thin FastAPI door in front of the
canonical gRPC core, permission-enforced *as the end user*, per-tenant Postgres, event-driven
cache invalidation, and a Vue SPA surface. Commentary becomes indexed content retrievable by
search and the RAG chat vector database. Permissions on a conversation are **derived from its
anchor document** — the service invents no access model of its own.

---

## 1. Scope & non-goals

### In scope
- Threaded comments anchored to a **file** or a specific **version** of a file.
- Thread lifecycle: `open → resolved`, with the resolving document version recorded (provenance).
- **@mention / flag**: raise a comment to a specific user's attention.
- **Review requests**: a requester asks one or more reviewers to review an anchor; acknowledgment
  and completion are tracked and notified back.
- A per-user **communication dashboard**: an *attention feed* (things needing you) and a
  *document-activity feed* (new/updated documents you may see).
- Exposure of commentary to full-text + vector search so the RAG pipeline can cite it.
- All doors: REST (primary), **MCP** (agents read/post comments under their own permissions),
  and the SPA.

### Non-goals (the Wave/Slack guardrails — from the roadmap anti-goal)
The following are **forbidden**, not merely deferred. No feature ships that violates them,
even behind a flag:
- **No channels, no DMs.** Discussion exists *only* where a document anchors it.
- **No presence, no typing indicators, no unread badges.** These stay forbidden.
- **No push notifications in v1.** Awareness is async-first: dashboard + (Phase 3) digest.
- The dashboard is a *projection of permissioned data the core already emits* — never a
  second, parallel messaging system.

**The one bounded real-time exception:** when two users have the *same document's* comment panel open
at once, new comments sync **live** between them (§10h) — collaborative comment editing, like a shared
document, not a notification/interrupt surface. It is confined to an already-open shared panel and
carries **only posted content**, never presence or typing indicators. Every *other* surface (dashboard,
file-list flags, digest) stays async and poll-based. This is the sole carve-out from "non-real-time".

### Deferred to a V2 specification
**Association of comments to entities *inside* a document — especially 3D model elements — is
out of scope for v1 and will be its own V2 specification.** This covers pinning a thread to a
sub-document region: an IFC/BIM element GUID, a PDF page + rectangle, a point-cloud/LAS bounding
box, a drawing coordinate, etc. It is deferred deliberately — it needs per-viewer coordinate models
and interaction design (xeokit picking, PDF overlays) that warrant separate treatment, and the core
has no sub-document addressing to build on (§4). **v1 anchors a thread to `(file_uid, version?)`
only.** The v1 data model and API are designed to extend to region anchoring additively (a future
`region` field on the anchor) so V2 layers on without migration pain — but v1 neither stores nor
interprets any in-document coordinate.

---

## 2. Glossary

| Term | Meaning |
|------|---------|
| **Anchor** | The `(file_uid, version?)` a thread is pinned to. `file_uid` is required. (In-document region anchoring is deferred to V2.) |
| **Thread** | An ordered set of comments on one anchor, with an `open`/`resolved` lifecycle. |
| **Comment** | One authored message in a thread. Immutable content; edits are versioned & audited. |
| **Mention / flag** | A comment field targeting a user, raising the comment to their attention. |
| **Review request** | A requester → reviewer(s) ask, tracked through `requested → acknowledged → completed`. |
| **Notification** | A per-user attention record (mention, review, reply, resolution) surfaced on the dashboard. |
| **Anchor ACL** | The effective ACL of the anchor `file_uid` in the core. **The sole authority for who may see/act on a thread.** |

---

## 3. Architecture & placement

A new sibling service, structurally a twin of CSAI and `ldap_manager`.

```
                         ┌─────────────────────────── Vue SPA (frontend) ──────────────────────────┐
                         │  Dashboard view · inline Thread panel (in Preview / FileBrowser) · MCP  │
                         └───────────────┬──────────────────────────────────────────┬──────────────┘
              /discuss (Vite/nginx proxy)│                                          │ /api, /csai
                                         ▼                                          ▼
                            ┌───────────────────────────┐            ┌────────────────────────────┐
                            │  discussion (FastAPI)     │  gRPC as   │  http_bridge / CSAI / core │
                            │  :8094                    │──user────▶ │  ACL enforced in the core  │
                            │  · REST + MCP door        │            └────────────────────────────┘
                            │  · own Postgres (pgvector)│  introspect ▲
                            │  · consumes fileengine    │  bearer ────┘ (CSAI_BRIDGE_URL pattern)
                            │    events (Redis stream)  │
                            │  · emits discussion events│──▶ Redis stream  (consumed by Phase 3 digest)
                            └───────────────────────────┘
```

**Decisions (mirroring the existing services):**

| Concern | Decision |
|--------|----------|
| Language / framework | Python + FastAPI, `create_app()` factory + `build_app()` pure-wiring split (CSAI convention). |
| Package | `src/discussion/…`, files kept **under ~500 lines**, split by responsibility. |
| HTTP port | **8094** (8090 bridge · 8092 CSAI · 8093 ldap_manager · **8094 discussion**). |
| Frontend proxy | `/discuss` → `http://localhost:8094` in `vite.config.ts` (and nginx in prod). |
| Env prefix | `DISC_*` for service-specific config; shared `FILEENGINE_*` reused verbatim. |
| Persistence | Own Postgres DB, per-tenant schema `tenant_<tenant>` (CSAI `schema.py`/`db.py` pattern), pgvector + pg_trgm. |
| Core access | `fileengine.ManagedFiles` via a `client_for(identity)` / `agent_client()` split (CSAI `core_client.py`). |
| Trust boundary | **Zero enforcement logic here.** Every core call carries the resolved end-user `AuthenticationContext`; the core decides. Bridge/impersonation rules from the roadmap apply. |

**Why not put threads inside the core?** The core is the canonical file substrate; discussion is
a *projection* on top of it (roadmap engineering convention: "new capabilities are projections of
the existing core"). Threads change far faster than files and need their own indexes, so they live
in the discussion service's DB — but they **borrow the core's ACL** for every visibility decision,
so there is still exactly one access model.

---

## 4. Data model

Per-tenant schema `tenant_<tenant>` (identical provisioning approach to CSAI's `schema.py`;
`connect_for_tenant()` scopes `search_path`). Illustrative DDL — final column sets settle in
the first migration:

```sql
CREATE EXTENSION IF NOT EXISTS vector;    -- comment embeddings for RAG
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- fuzzy comment search

-- A thread is pinned to an anchor. file_uid is the ACL authority.
CREATE TABLE threads (
    id            TEXT PRIMARY KEY,               -- app-generated id
    file_uid      TEXT NOT NULL,                  -- anchor; the core node whose ACL governs this thread
    version       TEXT NOT NULL DEFAULT '',       -- optional core version_timestamp; '' = "current"
    -- NOTE: in-document region anchoring (IFC element GUID, PDF page/rect, LAS bbox) is deferred
    -- to the V2 spec; it will add a nullable `region JSONB` column here without further migration.
    title         TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved')),
    resolved_by   TEXT,                           -- user who resolved
    resolved_version TEXT,                         -- version that addressed it (provenance → Phase 1)
    opened_by     TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    anchor_stale  BOOLEAN NOT NULL DEFAULT false  -- set when a newer file version exists (file.updated event)
);
CREATE INDEX idx_threads_file ON threads (file_uid, status);

CREATE TABLE comments (
    id            TEXT PRIMARY KEY,
    thread_id     TEXT NOT NULL REFERENCES threads (id) ON DELETE CASCADE,
    author        TEXT NOT NULL,
    body          TEXT NOT NULL,                  -- Markdown, constrained subset (see §4a)
    body_text     TEXT NOT NULL DEFAULT '',       -- plaintext projection (markup stripped) for FTS + embeddings
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    edited_at     TIMESTAMPTZ,                    -- edits allowed only by the author; original kept in comment_revisions
    deleted       BOOLEAN NOT NULL DEFAULT false, -- author soft-delete; body tombstoned, audit retained
    redacted      BOOLEAN NOT NULL DEFAULT false, -- admin moderation (§5b): display masked, original moved to redactions
    redacted_by   TEXT,
    redacted_at   TIMESTAMPTZ,
    redacted_reason TEXT,
    fts           tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(body_text,''))) STORED
);
CREATE INDEX idx_comments_thread ON comments (thread_id, created_at);
CREATE INDEX idx_comments_fts ON comments USING gin (fts);

CREATE TABLE comment_revisions (            -- immutable edit history (substrate = versioned)
    comment_id TEXT NOT NULL REFERENCES comments (id) ON DELETE CASCADE,
    body       TEXT NOT NULL,
    edited_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Protected store of pre-redaction content (§5b). Retained forever; readable only by
-- administrators/auditors. On redaction the original body is moved here and the comment's
-- displayed body/body_text are masked and its comment_chunks removed (de-indexed).
CREATE TABLE redactions (
    comment_id   TEXT NOT NULL REFERENCES comments (id) ON DELETE CASCADE,
    original_body TEXT NOT NULL,               -- the exact Markdown that was said
    redacted_by  TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    redacted_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE mentions (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    comment_id    TEXT NOT NULL REFERENCES comments (id) ON DELETE CASCADE,
    thread_id     TEXT NOT NULL,
    target_user   TEXT NOT NULL,                  -- MUST have READ on the thread's file_uid (see §5 invariant)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_mentions_target ON mentions (target_user, created_at DESC);

CREATE TABLE review_requests (
    id            TEXT PRIMARY KEY,
    file_uid      TEXT NOT NULL,                  -- anchor (ACL authority)
    version       TEXT NOT NULL DEFAULT '',
    thread_id     TEXT,                           -- optional originating thread
    requester     TEXT NOT NULL,
    reviewer      TEXT NOT NULL,                  -- one row per reviewer
    status        TEXT NOT NULL DEFAULT 'requested'
                  CHECK (status IN ('requested','acknowledged','completed','declined')),
    outcome       TEXT,                           -- 'approved' | 'changes' | free text on completion
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    acknowledged_at TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ
);
CREATE INDEX idx_reviews_reviewer ON review_requests (reviewer, status);
CREATE INDEX idx_reviews_requester ON review_requests (requester, status);

-- The attention feed backing store (one row per thing that wants a user's attention).
CREATE TABLE notifications (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id       TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN
                    ('mention','reply','review_requested','review_acknowledged',
                     'review_completed','thread_resolved')),
    file_uid      TEXT NOT NULL,                  -- for a read-time ACL re-check before display (§5)
    thread_id     TEXT,
    review_id     TEXT,
    actor         TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at       TIMESTAMPTZ                     -- "seen" state; NOT a real-time unread badge (see non-goals)
);
CREATE INDEX idx_notif_user ON notifications (user_id, read_at, created_at DESC);

-- Comment text vectorized for RAG. Keyed by the anchor file_uid so the existing
-- can_read(file_uid) gate applies unchanged (see §6).
CREATE TABLE comment_chunks (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    comment_id TEXT NOT NULL REFERENCES comments (id) ON DELETE CASCADE,
    file_uid   TEXT NOT NULL,                     -- anchor; the ACL key at retrieval time
    thread_id  TEXT NOT NULL,
    text       TEXT NOT NULL,
    embedding  vector(1024),                      -- dimension from DISC_EMBEDDING_DIMENSION
    fts        tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
);
CREATE INDEX idx_comment_chunks_hnsw ON comment_chunks USING hnsw (embedding vector_cosine_ops);

-- Durable projection of core file events (§8) so both the dashboard activity feed (§10a) and the
-- email digest (§11) can query "activity since T" and ACL-filter per viewer. Populated by the
-- event consumer from file.created/updated/restored; ACL is re-checked at read time by file_uid.
CREATE TABLE document_activity (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    file_uid   TEXT NOT NULL,
    event_type TEXT NOT NULL,                        -- 'created' | 'updated' | 'restored'
    version    TEXT NOT NULL DEFAULT '',
    name       TEXT NOT NULL DEFAULT '',
    path       TEXT NOT NULL DEFAULT '',
    actor      TEXT NOT NULL DEFAULT '',
    ts         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_activity_ts ON document_activity (ts DESC);
CREATE INDEX idx_activity_file ON document_activity (file_uid);

-- Per-user email-digest subscription (§11a). One row per user; self-service managed.
CREATE TABLE digest_subscriptions (
    user_id         TEXT PRIMARY KEY,
    cadence         TEXT NOT NULL DEFAULT 'off'
                    CHECK (cadence IN ('off','hourly','daily','weekly')),  -- user-chosen frequency
    send_hour_local SMALLINT NOT NULL DEFAULT 8   CHECK (send_hour_local BETWEEN 0 AND 23),
    send_dow        SMALLINT NOT NULL DEFAULT 1   CHECK (send_dow BETWEEN 0 AND 6),  -- weekly: 0=Sun
    timezone        TEXT NOT NULL DEFAULT 'UTC',
    scope           JSONB NOT NULL DEFAULT '{}',   -- {attention:true, activity:false, trees:[uid], tags:[..]}
    ai_summary      BOOLEAN NOT NULL DEFAULT false,
    quiet_if_empty  BOOLEAN NOT NULL DEFAULT true,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per (user, period) the sender processed — the idempotency guard (§11c). The UNIQUE key
-- makes double-send impossible across overlapping cron ticks / re-runs.
CREATE TABLE digest_deliveries (
    user_id     TEXT NOT NULL,
    period_key  TEXT NOT NULL,                       -- cadence bucket, e.g. 2026-07-04T15 | 2026-07-04 | 2026-W27
    status      TEXT NOT NULL CHECK (status IN ('sent','skipped_empty','error')),
    item_count  INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, period_key)
);
```

**Anchoring notes (grounded in the core's real model):**
- `file_uid` + `version` are the only identifiers the core understands. `version` is a
  lexicographically-sortable timestamp string from `ListVersions`.
- **The core has no sub-document model.** It addresses content by `file_uid` + `version` only —
  there is no page, range, or IFC-element addressing to build on. In-document/entity anchoring is
  therefore a genuine new capability, deferred to the **V2 specification** (see §1). When it lands,
  the region will be *service-side metadata* interpreted by the frontend viewers (PDF preview,
  xeokit BIM viewer) — the core stays unaware. v1 stores no such coordinate.
- A thread may pin to a specific `version` (immutable, stays put) or track "current". On a
  `file.updated` core event, threads on the *previous current* version get `anchor_stale = true`
  so the UI can show "commented on an earlier revision".

### 4a. Comment content format — constrained Markdown

Comments support **basic rich-text formatting**, stored as a **constrained Markdown subset** — not
HTML, not a proprietary rich-text blob. Markdown is the right substrate here: it is human-readable
as raw text (greppable, diff-able in `comment_revisions`, portable through every door), and CSAI
already treats extracted content as Markdown — so commentary indexes and renders on the exact same
footing as document text.

**Allowed formatting (the `etc.` made explicit):**

| Feature | Markdown | Notes |
|---------|----------|-------|
| Bold / italic | `**bold**`, `*italic*` | |
| Inline code | `` `code` `` | |
| Code block | ` ```lang … ``` ` | fenced; optional language hint for highlighting |
| Bulleted / numbered list | `- item` / `1. item` | nesting allowed |
| Blockquote | `> quote` | |
| Link | `[text](url)` | rendered `rel="noopener noreferrer"`, external `http(s)` only |
| Line breaks / paragraphs | blank line | |

**Deliberately excluded (v1):** raw/inline HTML, images & embeds, tables, headings (a comment is
not a document — heading levels belong to canon, not chatter), and arbitrary URL schemes
(`javascript:`, `data:` are stripped). `@mentions` are their own field/syntax (§5.1), not free
Markdown links.

**Three representations, one source of truth:**
- `comments.body` — the **raw Markdown** the author typed. The canonical stored form; the only thing
  the editor round-trips and `comment_revisions` versions.
- **Rendered HTML** — produced **at render time** (server- or client-side) from `body` through a
  Markdown renderer feeding a strict **allowlist sanitizer**: only the tags/attributes for the
  features above survive; everything else (scripts, event handlers, unknown schemes) is dropped.
  Rendered HTML is **never stored** and never trusted from the client — untrusted Markdown → XSS is
  the obvious risk, closed by sanitizing on the way out (see §13).
- `comments.body_text` — a **plaintext projection** (markup stripped) the service computes on
  write, used for FTS and embeddings so search matches on words, not backticks or asterisks
  (§6). Same treatment flows into `comment_chunks.text`.

The same constrained-Markdown contract applies to a **thread's opening body** and a **review
request's message**; all authored prose in this service shares one format and one sanitizer.

---

## 5. Permission model — derived, never invented

**Golden rule:** the discussion service holds **zero** enforcement logic. Every visibility or
mutation decision maps to a core permission check on the anchor `file_uid`, executed **as the
requesting user** (`ManagedFiles(user_name=…, user_roles=…, tenant=…)`), i.e. `CheckPermission` /
`GetEffectivePermissions`. The core's tiered, read-by-default, parent-traversal ACL (including the
`everyone`/`OTHER` alias and `DENY`-wins semantics) is the whole model.

| Action | Core permission required on `file_uid` |
|--------|----------------------------------------|
| List/read threads & comments on a document | **READ** |
| Open a thread; post a comment/reply; @mention; raise a review request | **READ** — *decided*: PR-review semantics; a reviewer need not be able to edit the file |
| Resolve / reopen a thread | Thread `opened_by`, **or** anyone with **WRITE**, **or** an assigned reviewer — *decided*: WRITE can resolve any thread |
| Edit / delete **own** comment | Author only (edits versioned in `comment_revisions`; deletes tombstone + audit) |
| Edit / delete **another user's** comment | Never — no user, not even a WRITE-holder, may alter another's words (append-only canon) |
| **Redact** any comment (moderation) | **Tenant administrator** only — masks the *displayed* content and de-indexes it (§6); the original is **retained, never destroyed** (§5b). A redaction, not a delete |
| Acknowledge / complete a review request | The named `reviewer` only |

**Two ACL-safety invariants (the analogue of CSAI's retrieval-time gating):**

1. **A mention/flag can never leak a document's existence.** The author may address *any* email —
   there is no pre-filtered "eligible users" picker (that discovery feature is deferred, §16). Instead,
   **on submit** the service checks each referenced target's READ on the anchor `file_uid` (core
   `CheckPermission` as that target). A target **without** READ is **rejected and error-marked**: the
   submit returns a per-reference validation error identifying which addressed users lack access, and
   no mention/assignment is persisted for them (the author can remove or replace the reference and
   resubmit). You cannot flag someone into a document they can't see — same principle as "never
   notified about a file you can't see". Applies identically to `@mention` and `reviewer` assignment.
2. **Every read path re-checks at query time.** Threads, comments, and notifications are filtered
   through `can_read(file_uid)` at fetch time (over-fetch → filter, exactly like CSAI's search/
   retrieval), not trusted from a stored snapshot. A `notifications` row therefore carries
   `file_uid` so it can be suppressed the instant the user loses access.

**Caching & invalidation:** reuse CSAI's `PermissionGate` pattern — per-`(tenant, user, file_uid)`
READ decisions cached ≤5 min, with **real-time eviction** driven by the core event stream
(`acl.changed` → evict resource; `role.assigned`/`role.member_removed` → evict member;
`role.deleted` → evict tenant). This is a direct port of CSAI's `cache_invalidation.py`.

**Agent identity:** an `agent_client()` (this service's `FILEENGINE_DISC_USER`) is used **only**
for indexing writes and internal maintenance — never to answer a user request. Per the roadmap
impersonation rule, no user-facing query ever runs as a privileged service account and filters
afterward.

### 5b. Administrator redaction (moderation without destruction)

Comments are append-only canon, but compliance needs a way to remove harmful or mistakenly-posted
content from view. The answer is **redaction, never deletion**: a **tenant administrator** (the
`require_tenant_admin` check — LDAP `cn=administrators,ou=<tenant>` membership, per `ldap_manager`)
may redact any comment, but the information is **retained forever, never destroyed**.

Redacting a comment (`redacted = true`, with `redacted_by`, `redacted_at`, `redacted_reason`):
- **Masks the displayed content** — readers see a "[redacted by administrator]" placeholder plus the
  reason and actor; `body`/`body_text` no longer surface the original text to normal readers.
- **De-indexes it** — the comment's `comment_chunks` rows are removed (§6), so redacted content can
  never resurface through search or the RAG chat.
- **Preserves the original** — the pre-redaction Markdown is moved to the protected `redactions`
  table (§4), readable only by administrators/auditors, and is **never purged** by any normal
  operation. This satisfies legal-hold: an auditor can always recover exactly what was said and by
  whom, while the wider tenant cannot.

Redaction is the *only* way anyone other than a comment's own author affects its content, and even
then it obscures rather than erases. (True destruction of information, if ever needed, would be a
separate, explicitly-gated retention operation — deliberately out of scope here, mirroring the
core's `CULL_VERSIONS` "destroy-data must be granted explicitly" stance.)

---

## 6. Indexing & RAG exposure

Roadmap requirement: "commentary becomes indexed content that can come up in search and exposed to
the chat vector database … retrievable by the RAG pipeline as context for future questions."

**Mechanism:** on comment create/edit/delete, the service chunks + embeds the **plaintext
projection** (`body_text`, Markdown markup stripped — §4a), not the raw Markdown, so search and
embeddings match on words rather than formatting characters. On **redaction** (§5b) and author
**delete**, the comment's `comment_chunks` rows are **removed**, so masked content can never resurface
through search or the RAG chat. Uses the pluggable embedding provider
(CSAI `providers/embeddings.py` pattern) into `comment_chunks`, **tagged with the anchor
`file_uid`**. Because a comment inherits its anchor's ACL, keying chunks by `file_uid` means the
*existing* `can_read(file_uid)` gate is exactly the right filter — **no new permission logic** is
needed at retrieval.

**Exposure to CSAI chat — DECISION: Option A (each service owns its data).**

The discussion service owns `comment_chunks` and exposes an internal, agent-authenticated
`POST /internal/retrieve` returning candidate `(text, file_uid, thread_id, score)`. CSAI's
`Retriever` calls it as an *additional source* alongside its own `chunks` and runs each hit through
its existing `PermissionGate.can_read`, then merges/ranks. Each service owns and migrates its own
schema; the trust boundary stays clean (ACL enforcement remains at CSAI's retrieval boundary, which
already gates by `file_uid`); and the discussion service can evolve its comment indexing without
CSAI schema coupling.

*Rejected alternative (Option B):* have the discussion service write comment chunks directly into
CSAI's tenant `chunks` table (`source='comment'`, `file_uid` = anchor). It needs zero CSAI retrieval
changes, but it makes one service write another's schema — a coupling that violates the
service-ownership boundary the rest of this design holds to. Not worth the short-term saving.

Either way **retrieval ACL-gating is by anchor `file_uid` and unchanged** — Option A simply keeps
that gate, and the data, on the right side of each service line.

Full-text/fuzzy comment search (`pg_trgm` + `tsvector`, CSAI `search.py` pattern) is served from
`comment_chunks`/`comments` directly by this service for the dashboard's in-thread search.

---

## 7. Authentication

Identical to CSAI/ldap_manager — one login at the bridge authenticates here too:

1. **Bridge bearer token** (primary): verify the `http_bridge`-issued token — locally via HS256 if
   `FILEENGINE_JWT_SECRET` is set, else introspect `DISC_BRIDGE_URL` + `/v1/auth/introspect`
   (cached, `DISC_BRIDGE_INTROSPECT_TTL`). Yields `Identity(user, roles, tenant)`.
2. **Service bearer** via `POST /auth/token` (LDAP bind → in-memory TTL `TokenStore`) for direct/API use.
3. **HTTP Basic** (LDAP bind per request) for scripts.

Tenant resolves from `X-Tenant` header or Host subdomain (`extract_tenant`). WebSocket/SSE endpoints
accept `?token=` as a fallback (CSAI chat pattern).

**MCP door:** the MCP server reaches this service (or embeds its client) so an agent can read and
post review comments **as its own resolved identity and roles** — the same `Identity` flows into the
same core checks. Agents are first-class thread participants, bound by the same ACL invariants
(including invariant §5.1 — an agent cannot mention a user into a doc that user can't see).

---

## 8. Event model

**Consumed** — from the core's `fileengine:events` Redis stream (CSAI `events.py`/`ingest.py`
consumer-group pattern; group name `DISC_EVENTS_GROUP`, at-least-once via `XREADGROUP`/`XACK`,
degraded sleep/poll on `WriteUnavailableError`). Ignore `is_rendition` events (avoid feedback):

| Core event | Discussion reaction |
|-----------|---------------------|
| `file.updated` | mark threads on the prior current version `anchor_stale = true` |
| `file.deleted` | soft-hide threads/notifications for that `file_uid` (retain for audit/undelete) |
| `file.restored` | re-instate hidden threads |
| `acl.changed`, `role.assigned`, `role.member_removed`, `role.deleted` | evict the `PermissionGate` cache (visibility recompute) |

**Emitted** — the service publishes its own events to a **`discussion:events`** stream (separate
from the core's, same Redis) for the **Phase 3 digest** service to consume. Envelope mirrors the
core's schema (`event_id`, `type`, `tenant`, `file_uid`, `actor`, `ts`, `schema`) plus
`thread_id`/`review_id`/`target_user` where relevant:

`comment.created` · `thread.opened` · `thread.resolved` · `review.requested` ·
`review.acknowledged` · `review.completed` · `mention.created`

Digest consumers **must** re-apply anchor-`file_uid` ACL filtering per recipient (roadmap Phase 3:
"a user is never notified about a file they can't see"). No per-event push is emitted to users in
v1 — these events feed the digest and the in-app dashboard only.

---

## 9. API surface (REST — illustrative)

All routes under the `/discuss` proxy; all require an authenticated `Identity`; all enforce §5.

```
GET    /healthz  /readyz                      # liveness/readiness (loopback-bind unauth per monitoring convention)
POST   /auth/token                            # LDAP-bind → service bearer (fallback auth)
GET    /whoami

# Threads on a document
GET    /files/{file_uid}/threads              # ?version= ?status=open|resolved ; READ-gated
POST   /files/{file_uid}/threads              # open a thread {version?, title, body}  (region: V2)
GET    /threads/{id}                          # thread + comments (re-checks READ)
POST   /threads/{id}/comments                 # reply {body, mentions:[email]}  (validate READ per target, error-mark: §5.1)
WS     /files/{file_uid}/live                  # live comment sync while the panel is open (?token=, §10h)
PATCH  /threads/{id}                          # resolve/reopen {status, resolved_version?}  (opener|WRITE|reviewer)
PATCH  /comments/{id}                         # edit own comment (versioned)
DELETE /comments/{id}                         # soft-delete own comment
POST   /comments/{id}/redact                  # tenant-admin only: mask + de-index; original → redactions {reason} (§5b)

# Review requests
POST   /files/{file_uid}/reviews              # {reviewers:[user], version?, thread_id?}  (validates reviewer READ)
POST   /reviews/{id}/acknowledge              # reviewer only → notifies requester
POST   /reviews/{id}/complete                 # reviewer only {outcome} → notifies requester
GET    /reviews?role=requester|reviewer&status=

# Dashboard feeds (the landing surface — §10)
GET    /dashboard/attention                   # merged, ACL-filtered notification feed for the caller
POST   /dashboard/attention/{id}/seen         # mark seen (state only; not a badge system)
GET    /dashboard/activity                    # new/updated documents the caller may see (from core events, READ-filtered)

# Per-file attention flags (batch — for the file browser row badges, §10e)
POST   /attention/flags                       # {file_uids:[…]} → {uid: {mentions, reviews}} for the caller (one round-trip)

# Email-digest self-service (§11a)
GET    /me/digest                             # read the caller's digest subscription
PUT    /me/digest                             # update {cadence, send_hour_local, send_dow, timezone, scope, ai_summary, quiet_if_empty}
POST   /me/digest/send-now                    # on-demand digest for the caller (rate-limited, §11c)

# Search within commentary
GET    /search?q=…                            # FTS/fuzzy over comments the caller may read

# Internal (agent-authenticated only)
POST   /internal/retrieve                     # RAG source for CSAI (Option A, §6)
```

**MCP tools** (thin wrappers over the same handlers, run as the agent's identity):
`list_threads(file_uid)`, `post_comment(thread_id, body, mentions?)`, `open_thread(file_uid, …)`,
`raise_review(file_uid, reviewers)`, `resolve_thread(thread_id, resolved_version?)`.

---

## 10. Frontend — the communication dashboard

Two surfaces, both mirroring existing SPA conventions (service client + Pinia store + view):

### 10a. The dashboard (landing surface)
A new authed view at route **`/dashboard`** (`requiresAuth`). **Decided: it is the post-login
landing** — `router/index.ts` redirects `/` → `/dashboard` (replacing the current `/` → `/files`);
the file browser remains a first-class destination reachable from the nav. Structurally like
`ChatView`/`TenantAdminView` (two-pane / tabbed). Two feeds, per the original stub:

1. **Attention feed** — messages/threads/reviews requesting the user's attention
   (`GET /dashboard/attention`): mentions, replies to your threads, review requests assigned to you,
   acknowledgments/completions of reviews you raised, resolutions. Each item **deep-links to the
   referenced comment** (§10f) — opening the preview (or the pure comment window, §10g) scrolled and
   highlighted to that comment, panel expanded.
2. **Document-activity feed** — new and updated documents the user is privileged to see
   (`GET /dashboard/activity`), served from the `document_activity` projection (§4, materialized from
   core events), READ-filtered per viewer. This is the Phase 3 "calm awareness" projection rendered
   in-app (no badges, no interrupt) — the same source the email digest (§11) draws from.

**Attention surfaces share one source.** The dashboard attention feed, the file-browser row flags
(§10e), the in-preview collapsed flag (§10b-i), and the email digest (§11) are four renderings of the
*same* underlying state — the `notifications`, `mentions`, and `review_requests` records (§4), always
re-checked with `can_read(file_uid)` per viewer. There is no per-surface attention logic to drift: a
mention or review that lights the file-list badge is the same row that lights the preview flag,
appears in the dashboard feed, and (on the receiver's cadence) the digest email — and it clears
everywhere at once when resolved. Consistency by construction.

**Refresh cadence — periodic poll, not push.** The dashboard feeds and their counts/metrics refresh
on a **~30-second poll** while the view is focused (interval configurable, e.g. `VITE_DISCUSS_POLL_MS`,
default 30000; paused when the tab is hidden, refreshed on refocus). This keeps the numbers current
without a per-event push surface — the dashboard stays async/poll-based; the **only** live channel is
the open comment panel (§10h). Deliberately **no websocket-push badge** here.

Client/store to add (matching `csaiClient.ts` + `stores/auth.ts`):
`src/services/discussionClient.ts` (axios, base `/discuss` via `VITE_DISCUSS_BASE`, attaches
`Authorization` + `X-Tenant`, 401 non-fatal like CSAI), `src/services/discussionService.ts`,
`src/stores/discussion.ts` (`attention`, `activity`, `filters`, `loading`; ~30 s focused poll).

### 10b. Inline thread panel (preview integration)
A `ThreadPanel.vue` component embedded in `PreviewView` (and reachable from the `FileDetailsDrawer`),
showing/adding threads for the open document (anchored to the file/version). This is where most
commenting actually happens (on the document), the dashboard being the aggregation surface. *Pinning
a thread to a specific in-document entity (xeokit BIM element, PDF page-region) is deferred to V2
(§1); v1 anchors at the document/version level.*

**Layout — user-configurable placement.** The panel's position within the preview is a user choice,
switchable from a control in the panel/preview header, with three modes:

| Mode | Behaviour |
|------|-----------|
| **Collapsed** | Panel hidden; only a compact toggle/tab remains, so the preview gets the full viewport. The toggle shows the **comment count** for the document (e.g. "Comments (N)") and, when the current user has something waiting on this document, an **attention flag** (§10b-i). Default when there are no threads. |
| **Pinned right** | Panel docked as a right-hand sidebar; the preview content reflows to the remaining width. Best for tall/portrait documents and wide screens. Default when threads exist on a wide screen. |
| **Pinned bottom** | Panel docked as a bottom drawer; the preview reflows to the remaining height. Best for wide/landscape content and 3D/BIM views. |

Details:
- **(10b-i) Collapsed count + attention flag.** The collapsed toggle always shows the document's
  **comment count** (open threads / total comments). Alongside it, an **attention flag** appears when
  the current user has something waiting **on this document**, distinguishing two states drawn from
  the user's own records (ACL-scoped, §5):
  - **Flagged** — the user is `@mention`ed in an unresolved thread here (a `mentions` row targeting
    them, §4). Icon/label e.g. "@ you".
  - **Needs review** — the user is the `reviewer` on a `review_requests` row for this document still
    in `requested`/`acknowledged` state (§4). Icon/label e.g. "Review requested".
  Both together show a combined marker with a count. The flag mirrors the dashboard attention feed
  (§10a) but scoped to the open document, so a reviewer/mentioned user sees the pull to act without
  expanding the panel. It clears as the underlying items resolve (mention's thread resolved, review
  completed) and re-checks READ so it never lights up for content the user can no longer see.
- **Persisted preference.** The chosen mode is remembered in `localStorage` (e.g.
  `fe.discuss.panelLayout`), mirroring the existing viewer-preference convention (e.g. the model
  viewer's `fe.model3d.sidebarCollapsed`). It is a per-browser UI preference, **not** server state —
  distinct from the digest subscription (§11a), which is durable per-user config.
- **Reflow, not overlay.** Pinned modes resize the preview surface rather than floating over it, so
  document content and 3D viewers stay fully visible; on mode change, viewer canvases are told to
  resize (the same pattern the model-viewer overlay uses when its sidebar toggles).
- **Responsive fallback.** On narrow viewports "pinned right" degrades to "pinned bottom" (or a
  full-screen sheet) automatically; the stored preference is retained and re-applied when width allows.
- **Switching never loses draft state** — an in-progress comment in the `CommentEditor` (§10c)
  survives a layout change (collapse/expand/redock).

### 10c. Comment editor
A `CommentEditor.vue` component (used by `ThreadPanel`, the new-thread form, and the review-request
message) providing **basic rich-text formatting** over the constrained-Markdown contract (§4a):

- **Toolbar** with bold, italic, inline code, code block, bulleted list, numbered list, blockquote,
  and link — plus their standard keyboard shortcuts (⌘/Ctrl-B, -I, etc.). The visible controls are
  exactly the §4a allowlist; nothing that would produce disallowed markup is offered.
- **Markdown in, Markdown out.** **Decided: a lightweight editor** — a textarea (or minimal
  contenteditable) with a format toolbar that inserts Markdown syntax and **serializes to Markdown** —
  not a heavyweight WYSIWYG/ProseMirror stack. The editor's value is the raw Markdown persisted to
  `comments.body`; the wire/storage format is always Markdown.
- **Rendering** uses a shared `renderMarkdown()` util (markdown renderer → strict allowlist
  sanitizer, §13) reused by both the editor preview and the read view, so authored and displayed
  output can never diverge and unsanitized HTML never reaches the DOM. **The SPA already ships
  `marked` (renderer) and `dompurify` (sanitizer)** — reuse that exact pipeline (`marked` →
  `DOMPurify.sanitize` with the §4a allowlist), matching whatever already backs the chat/report
  views, rather than adding a new dependency.
- Live character count against `DISC_MAX_COMMENT_CHARS`; paste is normalized to the allowed subset
  (e.g. pasted HTML is converted to Markdown or stripped, never injected raw).

### 10d. Digest settings panel
A self-service panel (in the dashboard, or Profile — mirroring `ldap_manager`'s `/me` self-service
and `ProfileView`) bound to `GET/PUT /me/digest` (§9), where the user sets their **notification
frequency** (`off` / `hourly` / `daily` / `weekly`), preferred time/day, scope (attention only, or
also activity for chosen trees/tags), AI-summary opt-in, and quiet-if-empty. Includes a **"Send me a
digest now"** button (`POST /me/digest/send-now`). This is the UI over the §11 email digest.

### 10e. File-list attention flags
The file browser (`FileBrowserView` / `useFileStore`) shows a per-row **attention flag** on any file
where the current user has something waiting — so a mention or review request is visible while
browsing, before opening the document. Same two states as the collapsed preview flag (§10b-i),
drawn from the same records:

- **Flagged** — the user is `@mention`ed in an unresolved thread on that file (`mentions`, §4).
- **Needs review** — the user is the `reviewer` on a `requested`/`acknowledged` `review_requests` row
  for that file (§4).

Rendered as a compact badge/icon in a list column (with a combined count when both apply), matching
the browser's existing row affordances; folders may roll up a subtree total (optional, later).

- **Batch fetch, not N calls.** For a directory listing the SPA makes **one** call —
  `POST /attention/flags {file_uids:[…]}` (§9) — returning per-uid `{mentions, reviews}` **only for
  the caller**, so a full folder is annotated in a single round-trip. Results are cached in
  `useFileStore` and refreshed on navigation.
- **ACL-scoped & consistent.** Flags are computed as the caller (a flag only exists on a file they can
  READ — guaranteed anyway, since a mention/review can't target a user without READ, §5.1) and clear
  as the underlying items resolve. This is the **same source** as the dashboard feed, preview flag,
  and digest (§10a) — the file-list is simply a fourth rendering of it.

### 10f. Deep-linking to a comment
Every attention surface links to a *specific* comment, not just a document. The canonical link — the
**thread/comment permalink** — carries the reference in the preview route:

```
/preview/:uid?thread=<thread_id>&comment=<comment_id>[&tenant=<tenant>]
```

Opening it: resolves the anchor, opens the preview (or the pure comment window, §10g, when the format
can't be previewed), **force-expands** the `ThreadPanel` (overriding the collapsed default),
**scrolls the target thread into view and briefly highlights** the referenced comment, and marks the
originating notification seen. `comment` alone is sufficient — its thread is resolved server-side; a
bare `thread` opens the thread at its top.

- **One link shape everywhere.** Dashboard attention items (§10a), file-list badge clicks (§10e), the
  collapsed preview flag (§10b-i), email-digest links (§11), mention/review notifications, and MCP
  responses all use this permalink — it is also the "thread permalink" the provenance schema stores
  (§12).
- **Permission at open time.** The link carries only uids, never content or a token. On open the view
  re-checks READ **as the viewer**; if denied (e.g. a link forwarded to someone without access), it
  shows an access-denied state with a request-access affordance — never the comment. Mirrors the
  roadmap's click-time link semantics.
- **Base URL.** Emails/agents build absolute links from `DISC_SPA_BASE_URL` (§14).

### 10g. Pure comment window (un-previewable formats)
Many file types have **no preview renderer** (proprietary CAD/binary formats with no rendition, etc.).
Discussion must still work, so for these the preview route renders a **pure comment window**: a
full-stage `ThreadPanel` with a document header (name, version, download, attention flag) but **no
document surface**.

- **Same entry point, graceful degrade.** `/preview/:uid` stays the single destination and permalink
  target; the view picks *document + panel* (§10b layout modes) when a renderer/rendition exists, or
  the *pure comment window* when it doesn't. Deep-links (§10f) therefore never break, and a file that
  later gains a rendition upgrades to the full preview automatically with no link rot. (A `/discuss/:uid`
  alias may route here explicitly.)
- The §10b layout modes don't apply here (there's nothing to reflow around) — the panel simply owns
  the window. Comment deep-linking, flags, and digest links behave identically.

### 10h. Real-time comment sync — the one live exception
The project is async-first and quiet by default (§1). The **single** deliberate exception: when two
or more users have the **same document's** comment panel open at once, comments post **live** between
them — a new reply, edit, redaction, resolution, or new thread appears within a second, so
co-reviewers aren't typing over a stale view. This is collaborative comment editing (think shared-doc
comments), scoped so it never becomes the interrupt machine the project refuses to build:

- **Only within an open shared panel.** A client subscribes to a file's live channel when its
  `ThreadPanel` / pure comment window (§10g) is open on that file, and unsubscribes on close or
  navigation. There is **no global live surface**: the dashboard, file-list flags (§10e), and digests
  stay poll/async — the §10a "no websocket-push badge" rule is unchanged.
- **Content only — no presence, no typing.** Subscribers receive *posted content* for that file
  (new/edited/redacted comment, new thread, resolve/reopen). They are **not** shown who else is
  viewing, nor typing indicators — both remain forbidden (§1). The line: broadcast what was actually
  said, never who is lurking or mid-keystroke.
- **ACL-gated per push.** Delivery re-checks READ for each subscriber (guards against a mid-session
  ACL change); no content reaches a socket whose user has lost access. A redaction (§5b) propagates
  as a *mask*, never re-broadcasting the original.
- **Enhancement, never required.** If the live channel is down, the panel silently falls back to its
  normal load plus a refresh-on-focus — nothing breaks. Local optimistic echo is reconciled against
  the authoritative insert (dedupe by comment id).

**Transport & fan-out:**
- A WebSocket endpoint `WS /files/{file_uid}/live` (bridge token via `?token=`, tenant via
  `?tenant=`/host), following the CSAI chat-WS auth pattern (§7). One subscription per open panel.
- On a comment mutation the service persists it, emits the durable `discussion:events` (for digests),
  **and** publishes to a per-file live channel — Redis pub/sub `discussion:live:<tenant>:<file_uid>` —
  so sockets on **any** service replica receive it (multi-instance safe). The live channel is
  best-effort/ephemeral; `notifications` + the DB remain the source of truth.
- Heartbeat + idle timeout close abandoned sockets; on reconnect the client re-syncs by fetching
  comments since its last-seen id. Guardrail: `DISC_LIVE_MAX_CONNS` caps concurrent sockets.

---

## 11. Email digest — user-configurable, cron-driven

This is the **email delivery half of the roadmap's Phase 3 digest** (the in-app attention/activity
feeds in §10 are the pull half). It is the async-first awareness surface: a **calm, periodic email**
whose **cadence the receiver chooses** — never a per-event notification. It directly inverts Slack's
incentive misalignment: the sender pays nothing to interrupt; here, nobody is interrupted at all,
because the receiver controls if and how often they hear from FileEngine.

**Anti-goal alignment (§1):** the digest is *scheduled and receiver-controlled*, so it is **not**
the "push notifications / real-time interrupt surface" the non-goals forbid. One email per period,
opt-in, per the receiver's frequency. A bot posting "Alice updated Q3_Deck.pdf" the moment it happens
is exactly what this design refuses to build.

### 11a. User-configurable subscription

Each user, per tenant, owns a digest subscription (`digest_subscriptions`, §4), managed through
self-service settings (§9 `/me/digest`, §10 dashboard panel). Configurable:

- **Frequency (`cadence`)** — the receiver's choice: **`off` · `hourly` · `daily` · `weekly`**. This
  is the "notification frequency" control; `off` is always available (calm by default is a first-class
  state, not a punishment).
- **Preferred time** — `send_hour_local` (0–23) for `daily`/`weekly`; `send_dow` (0–6) for `weekly`;
  interpreted in the user's `timezone`. (`hourly` ignores both.)
- **Scope** — what the digest covers (`scope` JSON): the **attention** stream (mentions, replies,
  review requests/acks/completions, resolutions — always on when enabled) and, optionally,
  **document activity** for subscribed trees (`file_uid`s) and/or tags the user follows.
- **AI summary** (`ai_summary`, opt-in) — a short natural-language rollup of the period
  ("3 new versions of the structural drawings; the LEED submittal was promoted; 2 comments await your
  reply"), generated best-effort via the CSAI chat provider.
- **`quiet_if_empty`** (default true) — send nothing when there is nothing; an empty period simply
  advances without an email. Calm by default.

### 11b. Digest content (per recipient, ACL-filtered)

Built from the durable **`notifications`** table (§4 — written as events occur) and the
**`document_activity`** projection (§4 — materialized from consumed core events, §8), covering
everything since the recipient's last delivered period. **Every item is re-checked with
`can_read(file_uid)` at send time, evaluated *as that recipient*** — the digest never mentions a file
the user can no longer see (the §5 golden rule and §5.1 invariant, applied to email). Links are
**comment permalinks** (§10f) into the **authenticated SPA** — opening the referenced comment in the
preview or pure comment window (§10g), re-checked at click time — built absolute from
`DISC_SPA_BASE_URL`. The email carries titles/snippets the recipient may already read, never
privileged content, and never a bearer token.

### 11c. The cron-triggered sender script

A standalone, **short-lived batch entrypoint** — `discuss-digest` (console script) →
`python -m discussion.digest` — **not** a long-running worker. It is **invoked hourly by cron** (or a
systemd timer, §11d); each run self-selects the users who are *due this hour* and sends them one
digest. The hourly tick is the clock; the **per-user `cadence` decides who actually receives one**:

- `hourly` → due every run (if there is content);
- `daily` → due on the run whose local hour == `send_hour_local`;
- `weekly` → due when local `(dow, hour)` == `(send_dow, send_hour_local)`;
- `off` → never.

**Algorithm (one run):**
1. **Take a run lock** (Postgres advisory lock, keyed per tenant/shard) so overlapping cron ticks or a
   slow prior run can't double-send.
2. For each tenant, select enabled subscriptions **due now** whose current period has **no
   `digest_deliveries` row** (the idempotency guard — see below).
3. For each due user, **acting as that user** (`client_for(identity)` with roles resolved from LDAP,
   per the impersonation rule — never a service account that filters afterward):
   a. Gather `notifications` + in-scope `document_activity` since `last period`, **ACL-filtered**
      via `PermissionGate.can_read`.
   b. If empty and `quiet_if_empty` → record a `skipped_empty` delivery and continue (advances the period).
   c. Optionally generate the AI summary (best-effort; failure downgrades to no-summary, never aborts).
   d. Render HTML + plaintext from a template (shared SMTP settings, the same `ldap_manager` uses for
      invite/reset mail).
   e. **Send via SMTP**, then **record a `digest_deliveries` row** `(user, tenant, period_key, sent_at,
      item_count, status)`.
4. **Release the lock.**

**Idempotency & crash-safety:** the `UNIQUE (user_id, period_key)` constraint on `digest_deliveries`
is the safety net — `period_key` is the canonical bucket for the user's cadence (e.g. `2026-07-04`
for daily, `2026-07-04T15` for hourly, `2026-W27` for weekly). A row is written per period, so a
re-run (crash, overlapping tick, manual re-invoke) **cannot double-send**; a crash mid-batch simply
resumes with the not-yet-recorded users next tick. This mirrors the CSAI ingest crash-safety ethos.
Email send + delivery-row write should be ordered so a send failure leaves the period **unmarked**
(retried next hour) with a bounded `error` status/retry count to avoid infinite retry on a poison user.

**On-demand** (`POST /me/digest/send-now`, §9) reuses the same builder for a single user, rate-limited,
bypassing cadence — the roadmap's "daily / weekly / on-demand".

### 11d. Scheduling & deployment

- **Production:** the schedule is owned by the **discussion Ansible role** in the scripts repo
  (`Rational-Boxes/fileengine_support_scripts`), consistent with the deploy convention — the
  discussion image exposes app + `discuss-digest` commands (as CSAI exposes app + worker), and the
  sender runs on an **hourly schedule** via a systemd timer / container cron shipped by that role.
  Host systemd units remain reserved for host concerns; this is a service task, so it lives with the
  service role.
- **Dev:** an hourly `crontab` line or a systemd **user** timer, mirroring the `csai-ingest` local
  convenience. Example:
  ```
  # crontab: hourly, on the hour
  0 * * * *  cd /…/discussion_threaded_communication && PYTHONPATH=src /usr/bin/python3.14 -m discussion.digest >> /tmp/discuss_digest.log 2>&1
  ```
  The script exits after each run; the schedule — not the process — is the periodicity.

**Config** (§14): `DISC_DIGEST_ENABLED`, `DISC_DIGEST_DEFAULT_CADENCE`, `DISC_DIGEST_BATCH_SIZE`,
`DISC_DIGEST_SEND_NOW_RATELIMIT`, `DISC_SPA_BASE_URL` (deep-link base), `DISC_AI_SUMMARY_ENABLED`,
plus SMTP (`DISC_SMTP_HOST/PORT/USER/PASSWORD`, `DISC_DIGEST_FROM`).

---

## 12. Provenance integration (Phase 1 hook)

- On resolve, `threads.resolved_version` records the version that addressed the discussion — a
  backward-provenance link ("this revision closed that thread").
- Phase 1's provenance schema gains a new **source type: `discussion_thread`**, so an AI report that
  drew on commentary cites the thread (permalink + anchor), and "which reports drew on the discussion
  of document X" is answerable. Thread text is retrievable by RAG (§6), so this is the same substrate,
  not a parallel store.

---

## 13. Non-functional requirements & conventions

- **Bridge/impersonation rules (roadmap):** thin door, zero enforcement logic, every core call as the
  end user. No broad-query-then-filter as a service account.
- **Tenant isolation:** per-tenant Postgres schema; `X-Tenant` scopes every request; caches keyed by tenant.
- **Fail-closed permissions:** a core check that errors denies (CSAI `PermissionGate` convention).
- **Comment sanitization (§4a):** authored Markdown is rendered through a strict allowlist sanitizer
  (`marked` → `DOMPurify`, the SPA's existing pipeline) on every render path; raw HTML, scripts,
  event handlers, and non-`http(s)` URL schemes are dropped. Rendered HTML is never stored or
  trusted from the client. Enforce `DISC_MAX_COMMENT_CHARS` on the raw Markdown at write time (a
  guardrail like CSAI's `CSAI_MAX_*`).
- **At-least-once events:** idempotent handlers; ack only after success; degraded sleep/poll on core read-only.
- **Digest sender (§11c):** ACL-filter every item **as the recipient** at send time (fail-closed,
  impersonation rule — never a service account that filters afterward); **idempotent per period**
  (`UNIQUE (user_id, period_key)`); a **run lock** prevents overlapping cron ticks double-sending;
  AI summary and SMTP are best-effort and must never abort the batch; a send failure leaves the
  period unmarked for retry, bounded to avoid poison-user loops.
- **Live sync (§10h):** best-effort and enhancement-only — a failed live channel never blocks a write
  or read; ACL is re-checked per push; cross-replica fan-out via Redis pub/sub; the DB/`notifications`
  stay the source of truth; concurrent sockets capped (`DISC_LIVE_MAX_CONNS`).
- **DB failover:** master/replica with read-only degradation, reusing CSAI's `db.py` circuit-breaker.
- **File size:** modules **< ~500 lines**; split by responsibility (roadmap engineering convention).
- **Monitoring:** `/healthz` `/readyz` (and a `/poolz` if pooled) **bind loopback-only** per the
  monitoring-port convention; app traffic arrives via the proxy.
- **Commits:** land as small, detailed commits documenting decisions + open questions so a new
  session can resume mid-phase.

---

## 14. Configuration (env)

Shared `FILEENGINE_*` reused verbatim (gRPC core, LDAP, Redis, JWT secret). Service-specific:

| Var | Purpose | Default |
|-----|---------|---------|
| `DISC_HTTP_HOST` / `DISC_HTTP_PORT` | bind | `127.0.0.1` / `8094` |
| `DISC_PG_HOST/PORT/DATABASE/USER/PASSWORD` | own Postgres (pgvector) | dev PG on `5434` |
| `DISC_PG_REPLICA_*`, `DISC_FAILOVER_*` | read-replica failover | disabled |
| `DISC_CORS_ORIGINS` | SPA origins (never `*`) | — |
| `DISC_BRIDGE_URL` / `DISC_BRIDGE_INTROSPECT_TTL` | bridge token introspection | — / 60s |
| `DISC_TOKEN_TTL` / `DISC_PERMISSION_CACHE_TTL` | service tokens / ACL cache (≤5 min) | 3600 / 300 |
| `FILEENGINE_DISC_USER` / `_PASSWORD` / `_TENANT` | agent identity (indexing only) | — |
| `DISC_EVENTS_GROUP` | core-stream consumer group | `discussion` |
| `DISC_EMITS_STREAM` | this service's event stream | `discussion:events` |
| `DISC_EMBEDDING_PROVIDER/MODEL/DIMENSION/BASE_URL/API_KEY` | comment embeddings (CSAI-compatible) | ollama / nomic / 1024 |
| `DISC_MAX_COMMENT_CHARS`, `DISC_MAX_RESULTS`, `DISC_DB_STATEMENT_TIMEOUT_MS` | guardrails | 10000 / 100 / 5000 |
| `DISC_LIVE_ENABLED` / `DISC_LIVE_HEARTBEAT_S` / `DISC_LIVE_MAX_CONNS` | live comment sync (§10h): switch / heartbeat / socket cap | true / 30 / 500 |
| `DISC_DIGEST_ENABLED` / `DISC_DIGEST_DEFAULT_CADENCE` | email digest master switch / default frequency for new users (§11) | true / `off` |
| `DISC_DIGEST_BATCH_SIZE` / `DISC_DIGEST_SEND_NOW_RATELIMIT` | sender batch size / on-demand rate limit | 200 / 1 per 10 min |
| `DISC_AI_SUMMARY_ENABLED` | allow opt-in AI digest summaries (CSAI chat provider) | false |
| `DISC_SPA_BASE_URL` | base URL for authenticated deep links in emails | — |
| `DISC_SMTP_HOST/PORT/USER/PASSWORD`, `DISC_DIGEST_FROM` | outbound mail (as `ldap_manager` invite/reset mail) | — |

---

## 15. Milestones

1. **M0 — skeleton:** FastAPI app, config, three-path auth, per-tenant schema provisioning,
   `client_for`/`agent_client`, health/ready. (Twin of CSAI bootstrap.)
2. **M1 — threads & comments:** anchor to `file_uid`/`version`; open/reply/resolve; READ/WRITE
   gating via core checks; own-comment edit/delete with revision history. REST + tests.
3. **M2 — mentions, reviews & moderation:** the §5.1 address-anything-then-validate-on-submit
   mention check; review state machine; admin redaction (§5b, `redactions` table + de-index);
   `notifications` writes; emit `discussion:events`.
4. **M3 — indexing & RAG:** `comment_chunks` embed/store; comment search; CSAI retrieval integration
   (Option A internal retrieve endpoint, §6).
5. **M4 — dashboard, preview panel & file-list flags:** `/dashboard/attention` + `/dashboard/activity`;
   `document_activity` projection; SPA view, client/store; `ThreadPanel` in `PreviewView` with the
   collapsed / pinned-right / pinned-bottom layout modes (§10b); batch `POST /attention/flags` +
   file-browser row badges (§10e); comment permalink deep-linking (§10f) and the pure comment window
   for un-previewable formats (§10g); real-time comment sync in the open panel (§10h,
   `WS /files/{uid}/live` + Redis pub/sub fan-out).
6. **M5 — MCP door + provenance hook:** MCP tools as agent identity; `discussion_thread` provenance
   source type; resolving-version links.
7. **M6 — email digest (Phase 3 delivery):** `digest_subscriptions`/`digest_deliveries`;
   `/me/digest` self-service + settings panel (§10d); the hourly cron `discuss-digest` sender (§11c)
   with per-recipient ACL filtering, per-period idempotency, run lock, SMTP templates, opt-in AI
   summary; Ansible-role schedule (§11d).

Order matches the roadmap sequencing: M0–M5 land the Phase 2 substrate (threads, moderation, RAG,
dashboard); **M6 is the Phase 3 email digest**, built on the M2 `notifications` + M4
`document_activity` foundation. Chat-delivered digests (Slack/Teams/Matrix) remain later (P5/P6),
reusing the same builder.

---

## 16. Decisions & remaining scope

**Resolved for v1:**
1. **Landing route (§10a):** `/` redirects to `/dashboard`; the dashboard is the post-login landing
   (the file browser stays reachable from the nav).
2. **Comment altitude (§5):** **READ** can comment — PR-review semantics. (A stricter per-tenant
   WRITE-to-comment policy is a *possible* future toggle, not built in v1.)
3. **Resolution authority (§5):** **WRITE**-on-file may resolve any thread, alongside the thread
   opener and assigned reviewers.
4. **Mention validation (§5.1):** authors may address **any** email; the service checks READ **on
   submit** and **error-marks** any referenced user lacking access — no persisted mention for them,
   no eligible-user pre-filter.
5. **Moderation (§5b):** tenant administrators may **redact** (mask display + de-index) a comment;
   the original is **retained forever, never destroyed**. No one else may alter another's words.
6. **Comment editor (§10c):** a **lightweight** toolbar editor that serializes to Markdown (no
   heavyweight WYSIWYG); render via the SPA's existing `marked` + `dompurify`.
7. **RAG/CSAI exposure (§6):** **Option A** — internal retrieve endpoint; each service owns its data.
8. **Email digest (§11):** **user-chosen frequency** (`off`/`hourly`/`daily`/`weekly`), delivered by
   an **hourly cron** `discuss-digest` sender that self-selects due users; per-recipient ACL filtering,
   per-period idempotency, opt-in AI summary, quiet-if-empty. Email + in-app only — **no per-event
   push** (Phase 3 delivery; the Phase 2 substrate is a prerequisite).
9. **Preview panel layout (§10b):** the inline `ThreadPanel` in `PreviewView` is user-switchable
   between **collapsed / pinned-right / pinned-bottom**, persisted per-browser in `localStorage`;
   pinned modes reflow the preview (no overlay). Collapsed shows the **comment count** plus an
   **attention flag** when the user is @mentioned (flagged) or has a pending review here.
10. **File-list attention flags (§10e):** the file browser shows per-row **flagged** (@mention) /
   **needs-review** badges for the caller, fetched for a whole directory in one batch call
   (`POST /attention/flags`). Fourth rendering of the one shared attention source (§10a).
11. **Comment permalinks & pure comment window (§10f/§10g):** links carry a
   `?thread=&comment=` reference so the preview opens scrolled+highlighted to the exact comment
   (re-checked at open); un-previewable formats render a full-window pure comment window at the same
   `/preview/:uid` route, so deep-links never break and upgrade if a rendition later appears.
12. **Real-time comment sync (§10h):** the *one* bounded exception to non-real-time — while two users
   have the same file's panel open, comments sync live (`WS /files/{uid}/live`, Redis pub/sub,
   ACL-per-push, enhancement-only). Content only — still **no presence, no typing, no badges**; every
   other surface stays async.
13. **Dashboard refresh (§10a):** the dashboard feeds and metrics refresh on a **~30 s focused poll**
   (configurable, paused when hidden) — current numbers without a push surface. Poll, not push; the
   live channel is the open panel only (§10h).

**Deferred to a V2 specification:**
- **In-document / entity anchoring (§1):** pinning a thread to a sub-document region — IFC/BIM
  element GUID, PDF page/rect, point-cloud/LAS bbox, drawing coordinate — with per-viewer coordinate
  models and picking/overlay interaction. The v1 anchor `(file_uid, version?)` extends to it additively.
- **Mention target discovery (§5.1):** an eligible-user picker driven by a "who-can-read(`file_uid`)"
  lookup, as an ergonomic upgrade over v1's address-anything-then-validate-on-submit.

No v1-blocking questions remain open.
