# Discussion & Threaded Communication

FastAPI service for document-anchored threaded discussion and review requests over
the FileEngine gRPC core — permission-enforced *as the end user*, per-tenant
Postgres, event-driven. See [`SPECIFICATION.md`](SPECIFICATION.md).

Structurally a twin of `convert_search_ai` (CSAI) and `ldap_manager`.

## Status

Building milestone by milestone on stacked feature branches (see SPECIFICATION §15);
nothing merges to `main` until the service is complete.

- **M0 — skeleton** ✓ FastAPI app, config, three-path auth (bridge bearer / service
  token / LDAP basic), per-tenant schema provisioning, `client_for`/`agent_client`,
  health/ready.
- **M1 — threads & comments** ✓ open/reply/resolve threads anchored to `file_uid`
  (+ version); READ/WRITE gating via core `CheckPermission` as the end user;
  own-comment edit (revision-versioned) / soft-delete; constrained-Markdown body
  with a stripped `body_text` projection. `GET/POST /files/{uid}/threads`,
  `GET/PATCH /threads/{id}`, `POST /threads/{id}/comments`, `PATCH/DELETE /comments/{id}`.
- **M2 — mentions, reviews & moderation** ✓ `@mention` with address-anything-then-
  validate-on-submit READ check (error-marked if a target lacks access, §5.1); review
  state machine (`raise → acknowledge → complete`, reviewer-gated, requester notified);
  admin redaction (`POST /comments/{id}/redact` — mask + de-index, original retained in
  `redactions`, audited, §5b); `notifications` writes (mention/reply/review/resolution);
  emits `discussion:events` to Redis (best-effort).
- **M3 — indexing & RAG** ✓ comment `body_text` chunked + embedded into `comment_chunks`
  on write, removed on delete/redact (best-effort, never fails the request); pluggable
  embedder (offline hash default; OpenAI-compatible/Ollama opt-in); `GET /search`
  (FTS/fuzzy, ACL-filtered as the caller, de-duplicated); `POST /internal/retrieve`
  (Option A, §6 — system-admin only, ANN candidates NOT filtered here so CSAI applies
  its own gate).
- **M4–M6** — dashboard/preview/live, MCP/provenance, email digest — follow.

## Layout

```
src/discussion/
  app.py          create_app()/build_app() factory + main() (uvicorn)
  api.py          the APIRouter (M0: /healthz /readyz /auth/token /whoami)
  config.py       env-driven Config (FILEENGINE_* shared, DISC_* specific)
  deps.py         identity / require_tenant_admin request dependencies
  ldap_auth.py    LDAP bind + role resolution (Identity)
  bridge_auth.py  accept http_bridge bearer tokens (introspect or local HS256)
  jwt_verify.py   local HS256 verification
  http_auth.py    per-request credential + tenant resolution
  token_store.py  in-memory TTL bearer tokens
  core_client.py  gRPC ManagedFiles bound to an identity (client_for/agent_client)
  _client.py      locate the reused `fileengine` python client
  db.py           psycopg connections, per-tenant search_path, replica failover
  schema.py       per-tenant DDL (SPECIFICATION §4 data model)
  failover.py     circuit-breaker primitive
migrations/0001_baseline.sql   DB-wide extensions (vector, pg_trgm)
```

## Run

```bash
pip install -e '.[dev]'          # or rely on the sibling python_interface checkout
cp .env.example .env             # then edit
psql "$DISC_PG_DSN" -f migrations/0001_baseline.sql   # once per database
discussion                       # serves on :8094 (uvicorn)
```

Locally the stack is brought up by `scripts/start_backend_services.sh` (infra +
core + bridges); this service reuses the same Postgres (`:5434`), LDAP (`:1389`),
and Redis (`:6379`).

## Test

Hermetic unit tests (no live services — LDAP/DB/core are stubbed or not touched):

```bash
PYTHONPATH=src python -m pytest src/tests -q
```

`live`-marked tests (added in later milestones) need LDAP + the gRPC core + Postgres.
