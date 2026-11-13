"""Application factory and ASGI entrypoint.

``uvicorn app.main:app`` is what the container runs.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__, agy
from .config import get_settings
from .errors import ApiError, error_body
from .routes import router

logger = logging.getLogger("groot")


def configure_logging(level: str) -> None:
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(getattr(logging, level.upper(), logging.INFO))
        return
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


class RequestContextMiddleware:
    """Pure-ASGI request id + access log.

    Deliberately *not* ``BaseHTTPMiddleware``: that wrapper interferes with
    streaming responses and client-disconnect propagation, which is exactly
    what an SSE agent endpoint depends on.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = MutableHeaders(scope=scope)
        request_id = headers.get("x-request-id") or uuid.uuid4().hex[:16]
        state = scope.setdefault("state", {})
        state["request_id"] = request_id

        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                out_headers = MutableHeaders(scope=message)
                out_headers["x-request-id"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "%s %s -> %s (%.1f ms) rid=%s",
                scope.get("method"),
                scope.get("path"),
                status_code,
                elapsed_ms,
                request_id,
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    app.state.sem = asyncio.Semaphore(settings.max_concurrency)

    binary_ok, binary_path = agy.dependency_status(settings)
    auth = agy.auth_status(settings)
    logger.info("groot-agy-api %s starting", __version__)
    logger.info("agy binary: %s (resolved=%s)", settings.agy_bin, binary_path)
    logger.info("workspace root: %s", settings.resolved_ws_root())
    logger.info("agy home: %s (token present=%s)", auth.agy_home, auth.authenticated)
    logger.info("max concurrency: %s", settings.max_concurrency)
    if not settings.auth_required:
        logger.warning("API_BEARER_TOKEN is empty: the HTTP API is unauthenticated")
    if not binary_ok:
        logger.error("agy binary not found: /v1/runs will fail until it is installed")
    if not auth.authenticated:
        logger.warning("agy is not logged in: run `docker compose exec -it api agy` once")

    yield

    logger.info("groot-agy-api stopping")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="groot · agy API",
        description=(
            "HTTP API around the Google Antigravity CLI (`agy`), running headless in a container. "
            "Credentials persist in a mounted volume at `$HOME/.gemini/antigravity-cli`."
        ),
        version=__version__,
        lifespan=lifespan,
    )

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-Id"],
            expose_headers=["X-Request-Id"],
        )

    app.add_middleware(RequestContextMiddleware)

    def _rid(request: Request) -> str | None:
        return getattr(request.state, "request_id", None)

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.details, _rid(request)),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error_body(
                "validation_error",
                "Request validation failed.",
                {"errors": jsonable_encoder(exc.errors())},
                _rid(request),
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error rid=%s", _rid(request))
        return JSONResponse(
            status_code=500,
            content=error_body("internal_error", "Unexpected server error.", None, _rid(request)),
        )

    app.include_router(router)
    return app


app = create_app()
