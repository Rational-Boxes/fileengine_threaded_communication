"""The HTTP / WebSocket API surface — one explicit router.

M0 (skeleton):
  GET  /healthz                    liveness
  GET  /readyz                     readiness (gRPC core + LDAP + Postgres reachable)
  POST /auth/token                 LDAP bind -> bearer token
  GET  /whoami                     resolved identity (user, roles, tenant)

Threads/comments/reviews/dashboard/digest/live land in M1–M6 (see SPECIFICATION §9).
build_app() wires the shared services onto app.state; handlers read them from there.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import __version__
from .config import Config
from .deps import identity
from .ldap_auth import Identity, authenticate

log = logging.getLogger("discussion.api")

router = APIRouter()


# --------------------------- readiness probes ------------------------------
def _check_ldap(config: Config) -> bool:
    try:
        if not config.agent_user or not config.agent_password:
            return False
        return authenticate(config, config.agent_user, config.agent_password).authenticated
    except Exception:
        return False


def _check_core(config: Config) -> bool:
    try:
        import grpc
        channel = grpc.insecure_channel(config.grpc_address)
        try:
            grpc.channel_ready_future(channel).result(timeout=2)
            return True
        finally:
            channel.close()
    except Exception:
        return False


def _check_db(config: Config) -> bool:
    try:
        from .db import connect
        conn = connect(config, readonly=True)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return True
        finally:
            conn.close()
    except Exception:
        return False


# ------------------------------- health ------------------------------------
@router.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "service": "discussion", "version": __version__}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    config: Config = request.app.state.config
    # The probes block (gRPC/LDAP/DB) — run them off the event loop.
    checks = {
        "core": await run_in_threadpool(_check_core, config),
        "ldap": await run_in_threadpool(_check_ldap, config),
        "db": await run_in_threadpool(_check_db, config),
    }
    ok = all(checks.values())
    return JSONResponse({"status": "ok" if ok else "degraded", "checks": checks},
                        status_code=200 if ok else 503)


# ------------------------------- auth --------------------------------------
@router.post("/auth/token")
async def auth_token(request: Request, body: dict = Body(...)) -> JSONResponse:
    config: Config = request.app.state.config
    tenant = (request.headers.get("x-tenant") or config.tenant).strip()
    username = (body or {}).get("username", "")
    password = (body or {}).get("password", "")
    ident = await run_in_threadpool(authenticate, config, username, password)
    if not ident.authenticated:
        return JSONResponse({"detail": "invalid credentials"}, status_code=401)
    from dataclasses import replace
    token = request.app.state.token_store.issue(replace(ident, tenant=tenant))
    return JSONResponse({"access_token": token, "token_type": "bearer"})


@router.get("/whoami")
def whoami(ident: Identity = Depends(identity)) -> dict:
    return {"user": ident.user, "roles": ident.roles, "tenant": ident.tenant,
            "is_admin": ident.is_admin}
