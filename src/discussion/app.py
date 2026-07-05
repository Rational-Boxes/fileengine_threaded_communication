"""FastAPI application factory for the discussion service.

The HTTP/WebSocket surface is in ``api.py`` (one explicit APIRouter); ``build_app``
wires shared services onto ``app.state`` and includes it:

  state.config            Config
  state.token_store       TokenStore (bearer tokens)
  state.bridge_verifier   BridgeTokenVerifier (accept http_bridge tokens)

``build_app`` stays pure (no .env side effects) so tests are hermetic; ``create_app``
loads ``./.env`` first for real launches.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from . import __version__, audit
from .activity_store import ActivityStore
from .api import router
from .bridge_auth import BridgeTokenVerifier
from .config import Config
from .dashboard_api import router as dashboard_router
from .directory import Directory
from .embeddings import build_embedder
from .events import EventPublisher
from .indexing import Indexer
from .live import LiveHub
from .live_api import router as live_router
from .notifications import NotificationStore
from .permissions import Permissions
from .reviews_api import router as reviews_router
from .reviews_store import ReviewStore
from .search import Searcher
from .search_api import router as search_router
from .store import ThreadStore
from .threads_api import router as threads_router
from .token_store import TokenStore
from .vectorstore import ChunkStore

log = logging.getLogger("discussion.app")


def build_app(config: Config | None = None, *, token_store: TokenStore | None = None,
              store: ThreadStore | None = None, permissions: Permissions | None = None,
              directory: Directory | None = None, events: EventPublisher | None = None,
              notifications: NotificationStore | None = None,
              reviews: ReviewStore | None = None, indexer: Indexer | None = None,
              searcher: Searcher | None = None, activity: ActivityStore | None = None,
              live: LiveHub | None = None) -> FastAPI:
    config = config or Config()
    audit.configure(config.audit_log_file)
    app = FastAPI(title="discussion", version=__version__)

    # Browser CORS for a SPA on another origin (off unless DISC_CORS_ORIGINS set).
    # Explicit origins (never "*") so credentialed bearer + X-Tenant requests work.
    if config.cors_origins:
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.state.config = config
    app.state.token_store = token_store or TokenStore(ttl_seconds=config.token_ttl)
    app.state.bridge_verifier = BridgeTokenVerifier(
        config.bridge_url, config.bridge_introspect_ttl, jwt_secret=config.jwt_secret)
    # Threads/comments (M1) + mentions/reviews/moderation (M2). Constructing these
    # is cheap and side-effect free (no DB/gRPC/Redis until a call is made).
    app.state.store = store or ThreadStore(config)
    app.state.permissions = permissions or Permissions(config)
    app.state.directory = directory or Directory(config)
    app.state.events = events or EventPublisher(config)
    app.state.notifications = notifications or NotificationStore(config)
    app.state.reviews = reviews or ReviewStore(config)
    # Indexing & RAG (M3): shared embedder + comment_chunks store.
    _embedder = build_embedder(config)
    _chunks = ChunkStore(config)
    app.state.indexer = indexer or Indexer(_embedder, _chunks)
    app.state.searcher = searcher or Searcher(_embedder, _chunks, app.state.permissions)
    # Dashboard feeds (M4a): the activity projection (populated by the consumer worker).
    app.state.activity = activity or ActivityStore(config)
    # Live comment sync + presence (M4b, §10h): in-process hub (cross-replica bridge
    # wired separately). Shares the cached permission checker for per-push ACL re-checks.
    app.state.live = live or LiveHub(config, app.state.permissions)

    app.include_router(router)
    app.include_router(threads_router)
    app.include_router(reviews_router)
    app.include_router(search_router)
    app.include_router(dashboard_router)
    app.include_router(live_router)
    return app


def create_app() -> FastAPI:
    """ASGI factory that loads ``./.env`` then builds the app — for launching via
    ``uvicorn discussion.app:create_app --factory`` or the ``discussion`` script."""
    from .config import load_dotenv
    load_dotenv()
    return build_app(Config())


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    app = create_app()
    cfg = app.state.config
    log.info("discussion %s — http=%s:%s core=%s", __version__, cfg.http_host, cfg.http_port,
             cfg.grpc_address)
    uvicorn.run(app, host=cfg.http_host, port=cfg.http_port)


if __name__ == "__main__":
    main()
