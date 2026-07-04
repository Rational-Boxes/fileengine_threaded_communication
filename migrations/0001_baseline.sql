-- Database-wide objects for the discussion service. Run once per database (the
-- per-tenant tables are provisioned by code — discussion.schema.ensure_tenant_schema).
--
--   psql "$DISC_PG_DSN" -f migrations/0001_baseline.sql
--
-- pgvector powers comment RAG retrieval (§6); pg_trgm powers fuzzy comment search.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
