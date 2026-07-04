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
- **No presence, no typing indicators, no unread badges, no real-time interrupt surface.**
- **No push notifications in v1.** Awareness is async-first: dashboard + (Phase 3) digest.
- The dashboard is a *projection of permissioned data the core already emits* — never a
  second, parallel messaging system.

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
    deleted       BOOLEAN NOT NULL DEFAULT false, -- soft-delete; body tombstoned, audit retained
    fts           tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(body_text,''))) STORED
);
CREATE INDEX idx_comments_thread ON comments (thread_id, created_at);
CREATE INDEX idx_comments_fts ON comments USING gin (fts);

CREATE TABLE comment_revisions (            -- immutable edit history (substrate = versioned)
    comment_id TEXT NOT NULL REFERENCES comments (id) ON DELETE CASCADE,
    body       TEXT NOT NULL,
    edited_at  TIMESTAMPTZ NOT NULL DEFAULT now()
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
  the obvious risk, closed by sanitizing on the way out (see §12).
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
| Open a thread; post a comment/reply; @mention; raise a review request | **READ** (PR-review semantics — a reviewer need not be able to edit the file) |
| Resolve / reopen a thread | Thread `opened_by`, **or** anyone with **WRITE**, **or** an assigned reviewer |
| Edit / delete **own** comment | Author only (edits versioned in `comment_revisions`; deletes tombstone + audit) |
| Edit / delete **another user's** comment | Never (not even WRITE-holders — canon is append-only; moderation is a separate, later concern) |
| Acknowledge / complete a review request | The named `reviewer` only |

**Two ACL-safety invariants (the analogue of CSAI's retrieval-time gating):**

1. **A mention/flag can never leak a document's existence.** Before a `@mention` or `reviewer`
   assignment is accepted, verify the *target* user has READ on the anchor `file_uid` (core
   `CheckPermission` as that target). If not, the mention is rejected — you cannot flag someone
   into a document they can't see. Same principle as "never notified about a file you can't see".
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

---

## 6. Indexing & RAG exposure

Roadmap requirement: "commentary becomes indexed content that can come up in search and exposed to
the chat vector database … retrievable by the RAG pipeline as context for future questions."

**Mechanism:** on comment create/edit/delete, the service chunks + embeds the **plaintext
projection** (`body_text`, Markdown markup stripped — §4a), not the raw Markdown, so search and
embeddings match on words rather than formatting characters. Uses the pluggable embedding provider
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
POST   /threads/{id}/comments                 # reply {body, mentions:[user]}  (validates each mention: §5.1)
PATCH  /threads/{id}                          # resolve/reopen {status, resolved_version?}
PATCH  /comments/{id}                         # edit own comment (versioned)
DELETE /comments/{id}                         # soft-delete own comment

# Review requests
POST   /files/{file_uid}/reviews              # {reviewers:[user], version?, thread_id?}  (validates reviewer READ)
POST   /reviews/{id}/acknowledge              # reviewer only → notifies requester
POST   /reviews/{id}/complete                 # reviewer only {outcome} → notifies requester
GET    /reviews?role=requester|reviewer&status=

# Dashboard feeds (the landing surface — §10)
GET    /dashboard/attention                   # merged, ACL-filtered notification feed for the caller
POST   /dashboard/attention/{id}/seen         # mark seen (state only; not a badge system)
GET    /dashboard/activity                    # new/updated documents the caller may see (from core events, READ-filtered)

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
A new authed view — proposed route **`/dashboard`** (`requiresAuth`), and the **post-login
landing** (change `/` redirect from `/files` → `/dashboard`; *open decision* in §15). Structurally
like `ChatView`/`TenantAdminView` (two-pane / tabbed). Two feeds, per the original stub:

1. **Attention feed** — messages/threads/reviews requesting the user's attention
   (`GET /dashboard/attention`): mentions, replies to your threads, review requests assigned to you,
   acknowledgments/completions of reviews you raised, resolutions. Each item deep-links to the anchor
   document's preview with the thread panel open.
2. **Document-activity feed** — new and updated documents the user is privileged to see
   (`GET /dashboard/activity`), sourced from the core event stream, READ-filtered. This is the
   Phase 3 "calm awareness" projection rendered in-app (no badges, no interrupt).

Client/store to add (matching `csaiClient.ts` + `stores/auth.ts`):
`src/services/discussionClient.ts` (axios, base `/discuss` via `VITE_DISCUSS_BASE`, attaches
`Authorization` + `X-Tenant`, 401 non-fatal like CSAI), `src/services/discussionService.ts`,
`src/stores/discussion.ts` (`attention`, `activity`, `filters`, `loading`; poll or SSE — **no
websocket-push badge**).

### 10b. Inline thread panel
A `ThreadPanel.vue` component embedded in `PreviewView` and the `FileDetailsDrawer`, showing/adding
threads for the open document (anchored to the file/version). This is where most commenting actually
happens (on the document), the dashboard being the aggregation surface. *Pinning a thread to a
specific in-document entity (xeokit BIM element, PDF page-region) is deferred to V2 (§1); v1 anchors
at the document/version level.*

### 10c. Comment editor
A `CommentEditor.vue` component (used by `ThreadPanel`, the new-thread form, and the review-request
message) providing **basic rich-text formatting** over the constrained-Markdown contract (§4a):

- **Toolbar** with bold, italic, inline code, code block, bulleted list, numbered list, blockquote,
  and link — plus their standard keyboard shortcuts (⌘/Ctrl-B, -I, etc.). The visible controls are
  exactly the §4a allowlist; nothing that would produce disallowed markup is offered.
- **Markdown in, Markdown out.** The editor's value is the raw Markdown persisted to `comments.body`
  — whether implemented as a WYSIWYG surface that serializes to Markdown or a textarea with a
  format toolbar is an implementation choice (§15), but the wire/storage format is always Markdown.
- **Rendering** uses a shared `renderMarkdown()` util (markdown renderer → strict allowlist
  sanitizer, §12) reused by both the editor preview and the read view, so authored and displayed
  output can never diverge and unsanitized HTML never reaches the DOM. **The SPA already ships
  `marked` (renderer) and `dompurify` (sanitizer)** — reuse that exact pipeline (`marked` →
  `DOMPurify.sanitize` with the §4a allowlist), matching whatever already backs the chat/report
  views, rather than adding a new dependency.
- Live character count against `DISC_MAX_COMMENT_CHARS`; paste is normalized to the allowed subset
  (e.g. pasted HTML is converted to Markdown or stripped, never injected raw).

---

## 11. Provenance integration (Phase 1 hook)

- On resolve, `threads.resolved_version` records the version that addressed the discussion — a
  backward-provenance link ("this revision closed that thread").
- Phase 1's provenance schema gains a new **source type: `discussion_thread`**, so an AI report that
  drew on commentary cites the thread (permalink + anchor), and "which reports drew on the discussion
  of document X" is answerable. Thread text is retrievable by RAG (§6), so this is the same substrate,
  not a parallel store.

---

## 12. Non-functional requirements & conventions

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
- **DB failover:** master/replica with read-only degradation, reusing CSAI's `db.py` circuit-breaker.
- **File size:** modules **< ~500 lines**; split by responsibility (roadmap engineering convention).
- **Monitoring:** `/healthz` `/readyz` (and a `/poolz` if pooled) **bind loopback-only** per the
  monitoring-port convention; app traffic arrives via the proxy.
- **Commits:** land as small, detailed commits documenting decisions + open questions so a new
  session can resume mid-phase.

---

## 13. Configuration (env)

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

---

## 14. Milestones

1. **M0 — skeleton:** FastAPI app, config, three-path auth, per-tenant schema provisioning,
   `client_for`/`agent_client`, health/ready. (Twin of CSAI bootstrap.)
2. **M1 — threads & comments:** anchor to `file_uid`/`version`; open/reply/resolve; READ/WRITE
   gating via core checks; own-comment edit/delete with revision history. REST + tests.
3. **M2 — mentions & review requests:** the §5.1 mention-safety check; review state machine;
   `notifications` writes; emit `discussion:events`.
4. **M3 — indexing & RAG:** `comment_chunks` embed/store; comment search; CSAI retrieval integration
   (ship Option B or A per §6).
5. **M4 — dashboard:** `/dashboard/attention` + `/dashboard/activity`; SPA view, `ThreadPanel`,
   client/store; core-event-fed activity feed.
6. **M5 — MCP door + provenance hook:** MCP tools as agent identity; `discussion_thread` provenance
   source type; resolving-version links.

Order matches the roadmap sequencing (P2 feeds the promotion loop; digest transport is P3, so the
dashboard's activity feed is the in-app half and the chat-delivered digest waits for P3/P5).

---

## 15. Open questions

1. **Landing route (§10a):** does `/` become `/dashboard`, or does the dashboard live alongside
   `/files` and the user chooses a default? Affects `router/index.ts`.
2. **Comment-permission altitude (§5):** confirm "READ can comment" (PR-review semantics) vs a
   stricter "WRITE to comment" for some tenants. Likely a per-tenant policy toggle — but v1 picks
   one default (proposed: READ).
3. **Resolution authority:** is WRITE-on-file sufficient to resolve *any* thread, or only the opener/
   reviewer? (Spec currently allows WRITE — revisit with QMS/compliance needs.)
4. **Mention target discovery:** the picker must only surface users who have READ on the anchor —
   does `ldap_manager`/core expose an efficient "who-can-read(file_uid)" query, or do we check
   per-candidate at submit time (safe but chatty)?
5. **V2 — in-document entity anchoring (§1):** the deferred region-anchoring capability (IFC/BIM
   element GUID, PDF page/rect, point-cloud/LAS bbox, drawing coordinate) is its own **V2
   specification**, covering per-viewer coordinate models and picking/overlay interaction. Not a v1
   open question — tracked here only so the v1 anchor stays additively extensible toward it.
6. **Retention/moderation:** comments are append-only canon; what is the tenant-admin story for
   redaction/legal-hold, given "no editing others' comments"?
7. **Editor implementation (§10c):** WYSIWYG-that-serializes-to-Markdown (e.g. Tiptap/Milkdown/
   ProseMirror) vs a lightweight textarea + format-toolbar + preview? Prefer reusing whatever
   Markdown renderer/sanitizer already backs the chat/report views before adding a dependency. The
   storage contract (constrained Markdown, §4a) is fixed regardless of the choice.

**Resolved:** RAG/CSAI integration is **Option A** (internal retrieve endpoint; each service owns its
data) — see §6.
```
