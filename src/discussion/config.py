"""Configuration for the discussion service, read from the environment.

A ``.env`` in the working directory is loaded automatically (without overriding
values already set in the environment), mirroring CSAI / the FileEngine MCP
server. ``FILEENGINE_*`` names are shared with the core / bridges / mcp / CSAI;
service-specific knobs use the ``DISC_*`` prefix.
"""
import os


def load_dotenv(path: str = ".env") -> None:
    if not os.path.isfile(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), _strip_value(val))


def _strip_value(val: str) -> str:
    """Parse a dotenv value: honor a surrounding quote, else drop an inline
    `` # …`` comment. A value that is *entirely* a comment yields ``""``."""
    val = val.strip()
    if val[:1] in ("'", '"'):
        q = val[0]
        end = val.find(q, 1)
        return val[1:end] if end != -1 else val[1:]
    if val.startswith("#"):
        return ""
    hi = val.find(" #")
    if hi != -1:
        val = val[:hi]
    return val.strip()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _first(*keys_and_default: str) -> str:
    *keys, default = keys_and_default
    for k in keys:
        v = os.environ.get(k)
        if v:
            return v
    return default


def _bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


class Config:
    def __init__(self) -> None:
        # --- gRPC core (shared with the bridges / mcp / CSAI) ---
        self.grpc_host = _env("FILEENGINE_GRPC_HOST", "localhost")
        self.grpc_port = _env("FILEENGINE_GRPC_PORT", "50051")
        self.grpc_address = f"{self.grpc_host}:{self.grpc_port}"

        # --- Tenant + this service's own agent identity ---
        # The agent identity is used only for internal maintenance (and, in M6, the
        # digest sender acts *as each recipient* via client_for — never this account
        # for a user-facing read). Every user request is evaluated as the end user.
        self.tenant = _env("FILEENGINE_DISC_TENANT", "default")
        self.agent_user = _first("FILEENGINE_DISC_USER", "FILEENGINE_LDAP_USER", "")
        self.agent_password = _first("FILEENGINE_DISC_PASSWORD", "FILEENGINE_LDAP_PASSWORD", "")

        # --- LDAP — the auth/role authority (mirrors CSAI + the bridges) ---
        self.ldap_uri = _env("FILEENGINE_LDAP_ENDPOINT", "ldap://localhost:1389")
        self.ldap_uri_replica = _env("FILEENGINE_LDAP_ENDPOINT_REPLICA", "")
        if not self.ldap_uri_replica and _bool("FILEENGINE_LDAP_REPLICA_ENABLED", False):
            self.ldap_uri_replica = "ldap://localhost:1389"
        self.ldap_replica_enabled = bool(self.ldap_uri_replica)
        self.ldap_domain = _env("FILEENGINE_LDAP_DOMAIN", "dc=rationalboxes,dc=com")
        self.ldap_user_base = _env("FILEENGINE_LDAP_USER_BASE", "ou=users,dc=rationalboxes,dc=com")
        self.ldap_tenant_base = _env("FILEENGINE_LDAP_TENANT_BASE", "ou=tenants,dc=rationalboxes,dc=com")
        self.ldap_bind_dn = _env("FILEENGINE_LDAP_BIND_DN", "cn=admin,dc=rationalboxes,dc=com")
        self.ldap_bind_password = _env("FILEENGINE_LDAP_BIND_PASSWORD", "admin")

        # --- This service's own Postgres (its own DB; per-tenant schema) ---
        self.pg_host = _env("DISC_PG_HOST", "localhost")
        self.pg_port = _int("DISC_PG_PORT", 5432)
        self.pg_database = _env("DISC_PG_DATABASE", "discussion")
        self.pg_user = _env("DISC_PG_USER", "fileengine_user")
        self.pg_password = _env("DISC_PG_PASSWORD", "fileengine_password")

        # Read-only replica (disconnect fault tolerance). Master is primary for all
        # reads + writes; when unreachable, reads fall back to the replica and writes
        # are rejected. OFF unless a replica host is configured; creds default to master.
        self.pg_replica_host = _env("DISC_PG_REPLICA_HOST", "")
        if not self.pg_replica_host and _bool("DISC_PG_REPLICA_ENABLED", False):
            self.pg_replica_host = "localhost"
        self.pg_replica_enabled = bool(self.pg_replica_host)
        self.pg_replica_port = _int("DISC_PG_REPLICA_PORT", self.pg_port)
        self.pg_replica_database = _env("DISC_PG_REPLICA_DATABASE", self.pg_database)
        self.pg_replica_user = _env("DISC_PG_REPLICA_USER", self.pg_user)
        self.pg_replica_password = _env("DISC_PG_REPLICA_PASSWORD", self.pg_password)
        self.failover_cooldown_s = _int("DISC_FAILOVER_COOLDOWN_S", 30)
        self.db_statement_timeout_ms = _int("DISC_DB_STATEMENT_TIMEOUT_MS", 5000)

        # --- HTTP surface ---
        self.http_host = _env("DISC_HTTP_HOST", "127.0.0.1")
        self.http_port = _int("DISC_HTTP_PORT", 8094)
        self.cors_origins = [o.strip() for o in _env("DISC_CORS_ORIGINS", "").split(",") if o.strip()]

        # --- Auth coordination (accept http_bridge bearer tokens) ---
        self.bridge_url = _env("DISC_BRIDGE_URL", "")
        self.bridge_introspect_ttl = _int("DISC_BRIDGE_INTROSPECT_TTL", 60)
        self.jwt_secret = _env("FILEENGINE_JWT_SECRET", "")
        self.token_ttl = _int("DISC_TOKEN_TTL", 3600)
        self.permission_cache_ttl = _int("DISC_PERMISSION_CACHE_TTL", 300)

        # --- Events (M2+): consume the core stream, emit our own ---
        self.redis_host = _env("FILEENGINE_REDIS_HOST", "localhost")
        self.redis_port = _int("FILEENGINE_REDIS_PORT", 6379)
        self.redis_password = _env("FILEENGINE_REDIS_PASSWORD", "")
        self.redis_db = _int("FILEENGINE_REDIS_DB", 0)
        self.events_stream = _env("FILEENGINE_EVENTS_STREAM", "fileengine:events")
        self.events_group = _env("DISC_EVENTS_GROUP", "discussion")
        self.emits_stream = _env("DISC_EMITS_STREAM", "discussion:events")

        # --- Comment indexing / embeddings (M3) — dimension fixes the schema ---
        self.embedding_dimension = _int("DISC_EMBEDDING_DIMENSION", 1024)
        self.embedding_provider = _env("DISC_EMBEDDING_PROVIDER", "ollama")
        self.embedding_model = _env("DISC_EMBEDDING_MODEL", "nomic-embed-text")
        self.embedding_base_url = _env("DISC_EMBEDDING_BASE_URL", "")
        self.embedding_api_key = _env("DISC_EMBEDDING_API_KEY", "")

        # --- Guardrails ---
        self.max_comment_chars = _int("DISC_MAX_COMMENT_CHARS", 10000)
        self.max_results = _int("DISC_MAX_RESULTS", 100)

        # --- Real-time (§10h) + presence, live channel (M4) ---
        self.live_enabled = _bool("DISC_LIVE_ENABLED", True)
        self.live_heartbeat_s = _int("DISC_LIVE_HEARTBEAT_S", 30)
        self.live_max_conns = _int("DISC_LIVE_MAX_CONNS", 500)
        self.presence_enabled = _bool("DISC_PRESENCE_ENABLED", True)
        self.presence_ttl_s = _int("DISC_PRESENCE_TTL_S", 45)
        self.presence_admin_invisible = _bool("DISC_PRESENCE_ADMIN_INVISIBLE", True)

        # --- Email digest (§11 / M6) ---
        self.digest_enabled = _bool("DISC_DIGEST_ENABLED", True)
        self.digest_default_cadence = _env("DISC_DIGEST_DEFAULT_CADENCE", "off")
        self.digest_batch_size = _int("DISC_DIGEST_BATCH_SIZE", 200)
        self.digest_send_now_ratelimit_s = _int("DISC_DIGEST_SEND_NOW_RATELIMIT_S", 600)
        self.ai_summary_enabled = _bool("DISC_AI_SUMMARY_ENABLED", False)
        self.spa_base_url = _env("DISC_SPA_BASE_URL", "")
        self.smtp_host = _env("DISC_SMTP_HOST", "")
        self.smtp_port = _int("DISC_SMTP_PORT", 587)
        self.smtp_user = _env("DISC_SMTP_USER", "")
        self.smtp_password = _env("DISC_SMTP_PASSWORD", "")
        self.digest_from = _env("DISC_DIGEST_FROM", "")

        # --- Audit (redaction / invisible-viewing trail, §5b/§10h) ---
        self.audit_log_file = _env("DISC_AUDIT_LOG_FILE", "")

    def _dsn(self, host: str, port: int, database: str, user: str, password: str) -> str:
        return f"host={host} port={port} dbname={database} user={user} password={password}"

    @property
    def pg_dsn(self) -> str:
        return self._dsn(self.pg_host, self.pg_port, self.pg_database, self.pg_user, self.pg_password)

    @property
    def pg_replica_dsn(self) -> str:
        return self._dsn(self.pg_replica_host, self.pg_replica_port, self.pg_replica_database,
                         self.pg_replica_user, self.pg_replica_password)
