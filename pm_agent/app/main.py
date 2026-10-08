"""
FastAPI application entry point.

Mounts Slack Bolt app via SlackRequestHandler and includes API routers.
Uses lifespan for DB connection pool management.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
from slack_bolt.async_app import AsyncApp

from pm_agent.app.api.health import router as health_router
from pm_agent.app.config import get_settings
from pm_agent.app.db.session import dispose_engine, get_engine
from pm_agent.app.logging_config import get_logger, setup_logging

logger = get_logger("main")


def create_slack_app() -> AsyncApp:
    """Create and configure the Slack Bolt async app."""
    settings = get_settings()

    app = AsyncApp(
        token=settings.slack_bot_token,
        signing_secret=settings.slack_signing_secret,
    )

    # ── Register Slack event handlers ──
    # These are imported and registered here to keep handler files thin.
    # Each handler module has a `register(app)` function.
    from pm_agent.app.api.slack_commands import register as register_commands
    from pm_agent.app.api.slack_events import register as register_events
    from pm_agent.app.api.slack_interactions import register as register_interactions

    register_events(app)
    register_commands(app)
    register_interactions(app)

    return app


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle — DB pool startup/shutdown."""
    settings = get_settings()
    setup_logging(settings.log_level)
    logger.info("PM Agent starting up...")

    # Ensure engine is created (validates DB connection config)
    get_engine()
    logger.info("Database engine initialized")

    yield

    # Shutdown
    logger.info("PM Agent shutting down...")
    await dispose_engine()
    logger.info("Database engine disposed")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="PM Agent",
        description="Slack-first project management assistant",
        version="0.1.0",
        lifespan=lifespan,
    )

    # ── Mount health router ──
    app.include_router(health_router)

    # ── Mount Slack Bolt ──
    slack_app = create_slack_app()
    handler = AsyncSlackRequestHandler(slack_app)

    @app.post("/slack/events")
    async def slack_events(request: Request):
        return await handler.handle(request)

    @app.post("/slack/commands")
    async def slack_commands(request: Request):
        return await handler.handle(request)

    @app.post("/slack/interactions")
    async def slack_interactions(request: Request):
        return await handler.handle(request)

    return app


def get_app() -> FastAPI:
    """
    Lazy app factory for uvicorn.

    Usage:
        uvicorn pm_agent.app.main:get_app --factory --reload --port 3000
      or:
        uvicorn pm_agent.app.main:app --reload --port 3000
    """
    return create_app()


# Lazy module-level app for uvicorn (only created when accessed)
class _LazyApp:
    """Delays app creation until first attribute access, avoiding import-time config errors."""

    def __init__(self):
        self._app = None

    def _ensure_app(self):
        if self._app is None:
            self._app = create_app()

    def __getattr__(self, name):
        self._ensure_app()
        return getattr(self._app, name)

    def __call__(self, *args, **kwargs):
        self._ensure_app()
        return self._app(*args, **kwargs)


app = _LazyApp()
