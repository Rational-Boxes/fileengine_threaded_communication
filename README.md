# Discussion & Threaded Communication

FastAPI service for document-anchored threaded discussion and review requests over
the FileEngine gRPC core — permission-enforced *as the end user*, per-tenant
Postgres, event-driven. See [`SPECIFICATION.md`](SPECIFICATION.md).

Structurally a twin of `convert_search_ai` (CSAI) and `ldap_manager`.

## Status

**M0 — skeleton** (see SPECIFICATION §15 milestones): FastAPI app, config, three-path
auth (bridge bearer / service token / LDAP basic), per-tenant schema provisioning,
`client_for`/`agent_client`, health/ready. M1–M6 follow on their own branches.

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
