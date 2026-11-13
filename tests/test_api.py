"""HTTP API tests. No real agy process is started; the wrapper is stubbed."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import agy, routes
from app.errors import ApiError


def _result(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_id": "run_test",
        "ok": True,
        "status": "SUCCESS",
        "exit_code": 0,
        "response": "42",
        "error": None,
        "conversation_id": "conv-1",
        "structured_output": {"answer": 42},
        "usage": {"input_tokens": 10, "output_tokens": 2},
        "num_turns": 1,
        "duration_seconds": 1.25,
        "agy_duration_seconds": 1.1,
        "stderr": None,
        "metadata": None,
    }
    base.update(overrides)
    return base


def _override(monkeypatch: pytest.MonkeyPatch, **env: Any) -> None:
    from app.config import get_settings

    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# probes
# ---------------------------------------------------------------------------


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-request-id"]


def test_readyz_reports_missing_credentials(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _override(monkeypatch, AGY_HOME=str(tmp_path / "no-credentials"))
    monkeypatch.setattr(agy, "dependency_status", lambda settings: (True, "/usr/local/bin/agy"))
    response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["agy_binary"]["ok"] is True
    assert body["checks"]["credentials"]["ok"] is False


def test_readyz_ready(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from app.config import get_settings

    home = tmp_path / "home"
    home.mkdir()
    _override(monkeypatch, AGY_HOME=str(home))
    (home / get_settings().agy_token_filename).write_text("token", encoding="utf-8")
    monkeypatch.setattr(agy, "dependency_status", lambda settings: (True, "/usr/local/bin/agy"))

    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["ready"] is True


# ---------------------------------------------------------------------------
# authentication
# ---------------------------------------------------------------------------


def test_auth_disabled_by_default(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agy, "probe_version", _async(lambda settings: "1.2.3"))
    assert client.get("/v1/info").status_code == 200


def test_auth_required(client: TestClient, secured: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agy, "probe_version", _async(lambda settings: "1.2.3"))

    assert client.get("/v1/info").status_code == 401
    bad = client.get("/v1/info", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "unauthorized"

    ok = client.get("/v1/info", headers={"Authorization": f"Bearer {secured}"})
    assert ok.status_code == 200
    assert ok.json()["service"] == "groot-agy-api"


def test_healthz_is_never_authenticated(client: TestClient, secured: str) -> None:
    assert client.get("/healthz").status_code == 200


# ---------------------------------------------------------------------------
# introspection
# ---------------------------------------------------------------------------


def test_info(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agy, "probe_version", _async(lambda settings: "1.2.3"))
    body = client.get("/v1/info").json()
    assert body["agy_version"] == "1.2.3"
    assert body["auth_required"] is False
    assert body["keyring_expected"] is False


def test_models(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.models import ModelEntry

    async def fake(settings):
        return [ModelEntry(id="gemini-3-pro", label="Gemini 3 Pro")], False, "raw"

    monkeypatch.setattr(agy, "list_models", fake)
    body = client.get("/v1/models").json()
    assert body["models"] == [{"id": "gemini-3-pro", "label": "Gemini 3 Pro"}]
    assert body["raw"] is None


def test_models_failure_is_structured(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(settings):
        raise ApiError(401, "agy_not_authenticated", "nope")

    monkeypatch.setattr(agy, "list_models", fake)
    response = client.get("/v1/models")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "agy_not_authenticated"


def test_auth_status_never_leaks_token(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from app.config import get_settings

    home = tmp_path / "home"
    home.mkdir()
    _override(monkeypatch, AGY_HOME=str(home))
    (home / get_settings().agy_token_filename).write_text("super-secret-token", encoding="utf-8")

    response = client.get("/v1/auth/status")
    assert response.status_code == 200
    assert "super-secret-token" not in response.text
    assert response.json()["authenticated"] is True


# ---------------------------------------------------------------------------
# buffered runs
# ---------------------------------------------------------------------------


def test_run_success(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake(run, settings, metadata=None):
        captured["run"] = run
        captured["metadata"] = metadata
        return _result(metadata=metadata)

    monkeypatch.setattr(agy, "run_buffered", fake)
    response = client.post(
        "/v1/runs",
        json={"prompt": "what is 6*7", "model": "gemini-3-pro", "metadata": {"trace": "abc"}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["response"] == "42"
    assert body["structured_output"] == {"answer": 42}
    assert captured["run"].output_format == "json"
    assert captured["run"].model == "gemini-3-pro"
    assert captured["metadata"] == {"trace": "abc"}


def test_run_agy_error_is_reported_in_body(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(run, settings, metadata=None):
        return _result(ok=False, status="ERROR", exit_code=2, response=None, error="bad model")

    monkeypatch.setattr(agy, "run_buffered", fake)
    response = client.post("/v1/runs", json={"prompt": "x"})
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["error"] == "bad model"


def test_run_requires_prompt(client: TestClient) -> None:
    response = client.post("/v1/runs", json={})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_run_rejects_unknown_fields(client: TestClient) -> None:
    response = client.post("/v1/runs", json={"prompt": "x", "nope": 1})
    assert response.status_code == 422


def test_run_rejects_conflicting_conversation_flags(client: TestClient) -> None:
    response = client.post(
        "/v1/runs",
        json={"prompt": "x", "conversation_id": "c1", "continue_conversation": True},
    )
    assert response.status_code == 422


def test_run_rejects_oversized_prompt(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _override(monkeypatch, MAX_PROMPT_CHARS=10)
    response = client.post("/v1/runs", json={"prompt": "x" * 11})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "prompt_too_large"


def test_run_rejects_timeout_above_cap(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _override(monkeypatch, MAX_TIMEOUT_SECONDS=60)
    response = client.post("/v1/runs", json={"prompt": "x", "timeout_seconds": 61})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "timeout_too_large"


def test_run_rejects_workdir_escape(client: TestClient) -> None:
    response = client.post("/v1/runs", json={"prompt": "x", "workdir": "../../etc"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] in {"workdir_outside_workspace", "workdir_not_found"}


def test_run_rejects_invalid_json_schema(client: TestClient) -> None:
    response = client.post("/v1/runs", json={"prompt": "x", "json_schema": "{not json"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_json_schema"


def test_run_returns_503_without_binary(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(run, settings, metadata=None):
        raise ApiError(503, "agy_not_installed", "missing")

    monkeypatch.setattr(agy, "run_buffered", fake)
    response = client.post("/v1/runs", json={"prompt": "x"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "agy_not_installed"


# ---------------------------------------------------------------------------
# streaming runs
# ---------------------------------------------------------------------------


def _fake_stream(*args: Any, **kwargs: Any):
    async def generator():
        yield ("run", {"run_id": "run_test", "workdir": "/workspace"})
        yield ("init", {"event": "init", "init": {"model": "gemini-3-pro"}})
        yield ("step_update", {"event": "step_update", "step_index": 0, "state": "DONE", "text_delta": "hi"})
        yield ("result", {"event": "result", "status": "SUCCESS", "response": "hi", "conversation_id": "c1"})
        yield ("end", {"run_id": "run_test", "exit_code": 0, "status": "SUCCESS", "duration_seconds": 0.2})

    return generator()


def test_stream_sse(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agy, "dependency_status", lambda settings: (True, "/usr/local/bin/agy"))
    monkeypatch.setattr(agy, "iter_stream", _fake_stream)

    with client.stream("POST", "/v1/runs/stream", json={"prompt": "hi"}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    assert "event: run" in body
    assert "event: init" in body
    assert "event: step_update" in body
    assert "event: result" in body
    assert "event: end" in body
    assert '"response":"hi"' in body


def test_stream_ndjson(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agy, "dependency_status", lambda settings: (True, "/usr/local/bin/agy"))
    monkeypatch.setattr(agy, "iter_stream", _fake_stream)

    with client.stream("POST", "/v1/runs/stream?format=ndjson", json={"prompt": "hi"}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        lines = [line for line in response.iter_lines() if line.strip()]

    events = [json.loads(line)["event"] for line in lines]
    assert events == ["run", "init", "step_update", "result", "end"]
    assert json.loads(lines[1])["payload"]["init"]["model"] == "gemini-3-pro"


def test_stream_requires_binary(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agy, "dependency_status", lambda settings: (False, None))
    response = client.post("/v1/runs/stream", json={"prompt": "hi"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "agy_not_installed"


def test_stream_emits_error_event_for_api_errors(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def raising(*args: Any, **kwargs: Any):
        async def generator():
            yield ("run", {"run_id": "run_test"})
            raise ApiError(401, "agy_not_authenticated", "log in")

        return generator()

    monkeypatch.setattr(agy, "dependency_status", lambda settings: (True, "/usr/local/bin/agy"))
    monkeypatch.setattr(agy, "iter_stream", raising)

    with client.stream("POST", "/v1/runs/stream", json={"prompt": "hi"}) as response:
        body = "".join(response.iter_text())

    assert "event: error" in body
    assert "agy_not_authenticated" in body


# ---------------------------------------------------------------------------
# concurrency guard
# ---------------------------------------------------------------------------


async def test_acquire_slot_returns_429_when_full(settings) -> None:
    sem = asyncio.Semaphore(1)
    await sem.acquire()
    with pytest.raises(ApiError) as exc:
        await routes._acquire_slot(sem, settings)
    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"] == "5"


async def test_acquire_slot_free(settings) -> None:
    sem = asyncio.Semaphore(1)
    await routes._acquire_slot(sem, settings)
    assert sem.locked()


async def test_acquire_slot_waits_then_times_out(settings) -> None:
    from app.config import Settings

    strict = Settings(queue_wait_seconds=0.05, max_concurrency=1)
    sem = asyncio.Semaphore(1)
    await sem.acquire()
    with pytest.raises(ApiError) as exc:
        await routes._acquire_slot(sem, strict)
    assert exc.value.code == "server_busy"


# ---------------------------------------------------------------------------
# encoding
# ---------------------------------------------------------------------------


def test_encode_sse_and_ndjson() -> None:
    sse = routes._encode("init", {"event": "init", "n": 1}, 3, "sse")
    assert sse.startswith("id: 3\nevent: init\ndata: ")
    assert sse.endswith("\n\n")

    keepalive = routes._encode("keepalive", {}, 4, "sse")
    assert keepalive == ": keepalive\n\n"

    ndjson = routes._encode("init", {"event": "init", "n": 1}, 3, "ndjson")
    assert json.loads(ndjson) == {"seq": 3, "event": "init", "payload": {"event": "init", "n": 1}}


def _async(fn):
    async def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    return wrapper
