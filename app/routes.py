"""HTTP routes."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import agy
from .agy import AgyRun
from .auth import require_token
from .config import Settings, get_settings
from .errors import ApiError
from .models import (
    AgentsResponse,
    AuthStatus,
    InfoResponse,
    ModelsResponse,
    RunRequest,
    RunResult,
    StreamFormat,
)
from .workspace import resolve_add_dirs, resolve_workdir

logger = logging.getLogger("groot.routes")

router = APIRouter()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _semaphore(request: Request) -> asyncio.Semaphore:
    sem = getattr(request.app.state, "sem", None)
    if sem is None:  # pragma: no cover - lifespan normally creates it
        sem = asyncio.Semaphore(get_settings().max_concurrency)
        request.app.state.sem = sem
    return sem


def _busy(settings: Settings) -> ApiError:
    return ApiError(
        429,
        "server_busy",
        "All agy worker slots are busy. Retry shortly.",
        {"max_concurrency": settings.max_concurrency},
        headers={"Retry-After": "5"},
    )


async def _acquire_slot(sem: asyncio.Semaphore, settings: Settings) -> None:
    if not sem.locked():
        await sem.acquire()
        return
    if settings.queue_wait_seconds <= 0:
        raise _busy(settings)
    try:
        await asyncio.wait_for(sem.acquire(), timeout=settings.queue_wait_seconds)
    except TimeoutError as exc:
        raise _busy(settings) from exc


def _validate_prompt(prompt: str, settings: Settings) -> None:
    if len(prompt) > settings.max_prompt_chars:
        raise ApiError(
            413,
            "prompt_too_large",
            "Prompt exceeds MAX_PROMPT_CHARS.",
            {"length": len(prompt), "max_prompt_chars": settings.max_prompt_chars},
        )
    if not prompt.strip():
        raise ApiError(422, "empty_prompt", "Prompt must contain non-whitespace characters.")


def _resolve_timeout(requested: int | None, settings: Settings) -> int:
    timeout = requested if requested is not None else settings.default_timeout_seconds
    if timeout > settings.max_timeout_seconds:
        raise ApiError(
            422,
            "timeout_too_large",
            "Requested timeout exceeds MAX_TIMEOUT_SECONDS.",
            {"requested": timeout, "max_timeout_seconds": settings.max_timeout_seconds},
        )
    return timeout


def _build_run(payload: RunRequest, settings: Settings, *, stream: bool) -> AgyRun:
    _validate_prompt(payload.prompt, settings)
    workdir = resolve_workdir(payload.workdir, settings)
    add_dirs = resolve_add_dirs(payload.add_dirs, settings)
    try:
        schema = payload.resolved_json_schema()
    except ValueError as exc:
        raise ApiError(422, "invalid_json_schema", str(exc)) from exc
    return AgyRun(
        prompt=payload.prompt,
        workdir=workdir,
        output_format="stream-json" if stream else payload.output_format,
        model=payload.model,
        effort=payload.effort,
        agent=payload.agent,
        conversation_id=payload.conversation_id,
        continue_conversation=payload.continue_conversation,
        json_schema=schema,
        skip_permissions=payload.skip_permissions,
        timeout_seconds=_resolve_timeout(payload.timeout_seconds, settings),
        add_dirs=tuple(add_dirs),
    )


def _encode(name: str, data: dict[str, Any], seq: int, fmt: StreamFormat) -> str:
    if fmt == "sse":
        if name == "keepalive":
            return ": keepalive\n\n"
        body = json.dumps(data, separators=(",", ":"), default=str)
        return f"id: {seq}\nevent: {name}\ndata: {body}\n\n"
    line = json.dumps({"seq": seq, "event": name, "payload": data}, separators=(",", ":"), default=str)
    return line + "\n"


# ---------------------------------------------------------------------------
# liveness / readiness
# ---------------------------------------------------------------------------


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness probe (503 until agy is installed and logged in)")
async def readyz(settings: Settings = Depends(get_settings)) -> JSONResponse:
    binary_ok, binary_path = agy.dependency_status(settings)
    root = settings.resolved_ws_root()
    auth = agy.auth_status(settings)
    checks = {
        "agy_binary": {"ok": binary_ok, "path": binary_path or settings.agy_bin},
        "workspace": {"ok": root.is_dir(), "path": str(root)},
        "credentials": {"ok": auth.authenticated, "path": auth.token_path},
    }
    ready = all(check["ok"] for check in checks.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "degraded", "ready": ready, "checks": checks},
    )


# ---------------------------------------------------------------------------
# introspection
# ---------------------------------------------------------------------------


@router.get("/v1/info", response_model=InfoResponse, dependencies=[Depends(require_token)])
async def info(settings: Settings = Depends(get_settings)) -> InfoResponse:
    binary_ok, _ = agy.dependency_status(settings)
    version = await agy.probe_version(settings) if binary_ok else None
    auth = agy.auth_status(settings)
    from . import __version__

    return InfoResponse(
        service="groot-agy-api",
        version=__version__,
        agy_bin=settings.agy_bin,
        agy_version=version,
        workspace_root=str(settings.resolved_ws_root()),
        default_workdir=settings.default_workdir,
        max_concurrency=settings.max_concurrency,
        auth_required=settings.auth_required,
        authenticated=auth.authenticated,
        keyring_expected=False,
    )


@router.get("/v1/auth/status", response_model=AuthStatus, dependencies=[Depends(require_token)])
async def auth_status(settings: Settings = Depends(get_settings)) -> AuthStatus:
    """Report whether agy has usable credentials. Token contents are never returned."""
    return agy.auth_status(settings)


@router.get("/v1/models", response_model=ModelsResponse, dependencies=[Depends(require_token)])
async def models(settings: Settings = Depends(get_settings)) -> ModelsResponse:
    entries, cached, raw = await agy.list_models(settings)
    return ModelsResponse(models=entries, cached=cached, raw=None if entries else raw)


@router.get("/v1/agents", response_model=AgentsResponse, dependencies=[Depends(require_token)])
async def agents(settings: Settings = Depends(get_settings)) -> AgentsResponse:
    entries, cached, raw = await agy.list_agents(settings)
    return AgentsResponse(agents=entries, cached=cached, raw=None if entries else raw)


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------


@router.post(
    "/v1/runs",
    response_model=RunResult,
    dependencies=[Depends(require_token)],
    summary="Run a prompt and wait for the full answer",
)
async def create_run(
    payload: RunRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> RunResult:
    run = _build_run(payload, settings, stream=False)
    sem = _semaphore(request)
    await _acquire_slot(sem, settings)
    try:
        logger.info(
            "run start workdir=%s model=%s agent=%s timeout=%ss",
            run.workdir,
            run.model,
            run.agent,
            run.timeout_seconds,
        )
        result = await agy.run_buffered(run, settings, metadata=payload.metadata)
    finally:
        sem.release()
    logger.info(
        "run done run_id=%s ok=%s exit=%s in %.2fs",
        result["run_id"],
        result["ok"],
        result["exit_code"],
        result["duration_seconds"],
    )
    return RunResult(**result)


@router.post(
    "/v1/runs/stream",
    dependencies=[Depends(require_token)],
    summary="Run a prompt and stream events (SSE or NDJSON)",
    response_class=StreamingResponse,
)
async def stream_run(
    payload: RunRequest,
    request: Request,
    format: StreamFormat = Query(default="sse", description="`sse` (default) or `ndjson`."),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    run = _build_run(payload, settings, stream=True)

    binary_ok, _ = agy.dependency_status(settings)
    if not binary_ok:
        raise ApiError(
            503,
            "agy_not_installed",
            "The agy CLI is not available inside this container.",
            {"agy_bin": settings.agy_bin},
        )

    sem = _semaphore(request)
    await _acquire_slot(sem, settings)

    async def event_source():
        seq = 0
        released = False
        try:
            async for name, data in agy.iter_stream(run, settings, metadata=payload.metadata):
                seq += 1
                yield _encode(name, data, seq, format)
        except ApiError as exc:
            seq += 1
            yield _encode(
                "error",
                {"code": exc.code, "message": exc.message, "details": exc.details},
                seq,
                format,
            )
        except asyncio.CancelledError:
            # Client disconnected; iter_stream already terminated the process.
            raise
        except Exception:
            logger.exception("unexpected streaming failure")
            seq += 1
            yield _encode(
                "error",
                {"code": "internal_error", "message": "Unexpected server error while streaming."},
                seq,
                format,
            )
        finally:
            if not released:
                released = True
                sem.release()

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    }
    media_type = "text/event-stream" if format == "sse" else "application/x-ndjson"
    try:
        return StreamingResponse(event_source(), media_type=media_type, headers=headers)
    except BaseException:
        sem.release()
        raise
