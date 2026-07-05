"""MCP door for the discussion service (SPECIFICATION §7 §9 / M5).

A FastMCP Streamable-HTTP server exposing thread/comment/review tools. Each request
authenticates separately (Basic → LDAP bind, or Bearer → token / bridge), and the
resolved identity is bound into a ContextVar for the duration of the request so the
tools act **as that agent** — never a privileged service account (§5 impersonation
rule). Mirrors the fileengine_mcp http_app pattern.

Launch: ``discuss-mcp-http`` (serves ``/mcp`` + ``/auth/token`` + ``/whoami``).
"""
from __future__ import annotations

import contextvars
import json
import logging
from typing import Optional

from .config import Config, load_dotenv
from .ldap_auth import Identity
from .mcp_tools import Components, ToolError, Toolset, build_components

log = logging.getLogger("discussion.mcp")

_current_identity: "contextvars.ContextVar[Optional[Identity]]" = contextvars.ContextVar(
    "discussion_mcp_identity", default=None)


def _ident() -> Identity:
    ident = _current_identity.get()
    if ident is None:  # pragma: no cover - middleware guarantees this on /mcp
        raise ToolError("authentication required")
    return ident


def build_server(toolset: Toolset):
    """A FastMCP server whose tools call ``toolset`` as the per-request identity."""
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    server = FastMCP("fileengine-discussion")
    read = ToolAnnotations(readOnlyHint=True)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)

    @server.tool(annotations=read)
    def list_threads(file_uid: str, status: str = "") -> list[dict]:
        """List discussion threads anchored to a file (optionally status=open|resolved)."""
        return toolset.list_threads(_ident(), file_uid, status or None)

    @server.tool(annotations=read)
    def get_thread(thread_id: str) -> dict:
        """Get a thread and its comments by id (READ-gated on the anchor file)."""
        return toolset.get_thread(_ident(), thread_id)

    @server.tool(annotations=write)
    def open_thread(file_uid: str, body: str, title: str = "", version: str = "") -> dict:
        """Open a new discussion thread on a file with an initial comment (Markdown body)."""
        return toolset.open_thread(_ident(), file_uid, body=body, title=title, version=version)

    @server.tool(annotations=write)
    def post_comment(thread_id: str, body: str, mentions: Optional[list[str]] = None) -> dict:
        """Reply to a thread. `mentions` are emails/uids validated to have READ (else rejected)."""
        return toolset.post_comment(_ident(), thread_id, body=body, mentions=mentions)

    @server.tool(annotations=write)
    def resolve_thread(thread_id: str, resolved_version: str = "") -> dict:
        """Resolve a thread (thread opener or a WRITE-holder), linking the addressing version."""
        return toolset.resolve_thread(_ident(), thread_id, resolved_version=resolved_version or None)

    @server.tool(annotations=write)
    def raise_review(file_uid: str, reviewers: list[str], version: str = "",
                     thread_id: str = "") -> list[dict]:
        """Request review of a file from one or more reviewers (validated to have READ)."""
        return toolset.raise_review(_ident(), file_uid, reviewers, version=version,
                                    thread_id=thread_id or None)

    return server


class AuthMiddleware:
    """Pure-ASGI auth: resolve the identity, bind the ContextVar, delegate."""
    _OPEN = {"/auth/token"}

    def __init__(self, app, config, store):
        self.app, self.config, self.store = app, config, store

    async def __call__(self, scope, receive, send):
        from .http_auth import extract_tenant, resolve_identity
        if scope["type"] != "http" or scope.get("path", "").rstrip("/") in {p.rstrip("/") for p in self._OPEN}:
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        tenant = extract_tenant(headers, headers.get("host", ""), self.config.tenant)
        bridge = getattr(self, "_bridge", None)
        identity = resolve_identity(headers.get("authorization", ""), tenant, self.config,
                                    self.store, bridge)
        if identity is None or not identity.authenticated:
            from starlette.responses import JSONResponse
            return await JSONResponse({"error": "authentication required"}, status_code=401)(
                scope, receive, send)
        token = _current_identity.set(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_identity.reset(token)


def build_http_app(config: Config, *, components: Optional[Components] = None, ttl_seconds: int = 3600):
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from .bridge_auth import BridgeTokenVerifier
    from .http_auth import decode_basic
    from .ldap_auth import authenticate
    from .token_store import TokenStore

    toolset = Toolset(components or build_components(config))
    server = build_server(toolset)
    app = server.streamable_http_app()
    store = TokenStore(ttl_seconds)
    bridge = BridgeTokenVerifier(config.bridge_url, config.bridge_introspect_ttl, jwt_secret=config.jwt_secret)
    app.state.config = config
    app.state.token_store = store

    async def token_endpoint(request: Request) -> JSONResponse:
        auth = request.headers.get("authorization", "")
        creds = decode_basic(auth)
        if creds is None:
            try:
                body = json.loads(await request.body() or b"{}")
            except json.JSONDecodeError:
                body = {}
            creds = (body.get("username", ""), body.get("password", ""))
        user, password = creds
        if not user or not password:
            return JSONResponse({"error": "missing credentials"}, status_code=400)
        identity = authenticate(config, user, password)
        if not identity.authenticated:
            return JSONResponse({"error": "authentication failed"}, status_code=401)
        return JSONResponse({"access_token": store.issue(identity), "token_type": "bearer"})

    async def whoami(_request: Request) -> JSONResponse:
        ident = _current_identity.get()
        if ident is None:
            return JSONResponse({"error": "authentication required"}, status_code=401)
        return JSONResponse({"user": ident.user, "roles": ident.roles, "tenant": ident.tenant})

    app.router.routes.append(Route("/auth/token", token_endpoint, methods=["POST"]))
    app.router.routes.append(Route("/whoami", whoami, methods=["GET"]))
    mw = AuthMiddleware
    mw._bridge = bridge  # type: ignore[attr-defined]
    app.add_middleware(mw, config=config, store=store)
    return app


def main_http() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    load_dotenv()
    config = Config()
    app = build_http_app(config)
    port = config.http_port + 1  # discussion :8094 → MCP :8095
    log.info("discussion MCP (Streamable-HTTP) on %s:%s/mcp", config.http_host, port)
    uvicorn.run(app, host=config.http_host, port=port)


if __name__ == "__main__":
    main_http()
