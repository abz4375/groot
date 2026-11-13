"""Shared test fixtures.

Environment is configured *before* the app is imported so that the cached
``Settings`` instance and the container-shaped defaults are test-shaped.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_WORKSPACE = Path(tempfile.mkdtemp(prefix="groot-ws-"))
(_WORKSPACE / "sub").mkdir(exist_ok=True)
(_WORKSPACE / "other").mkdir(exist_ok=True)
(_WORKSPACE / "file.txt").write_text("hello", encoding="utf-8")

os.environ["WORKSPACE_ROOT"] = str(_WORKSPACE)
os.environ["DEFAULT_WORKDIR"] = "."
os.environ["API_BEARER_TOKEN"] = ""
os.environ["DEFAULT_TIMEOUT_SECONDS"] = "60"
os.environ["MAX_TIMEOUT_SECONDS"] = "120"
os.environ["MAX_CONCURRENCY"] = "2"
os.environ["QUEUE_WAIT_SECONDS"] = "0"
os.environ["MODELS_CACHE_TTL_SECONDS"] = "0"
os.environ["AGY_HOME"] = str(_WORKSPACE / ".agy-home")
os.environ["LOG_LEVEL"] = "WARNING"


@pytest.fixture(scope="session")
def workspace() -> Path:
    return _WORKSPACE


@pytest.fixture()
def settings():
    from app.config import Settings, get_settings

    get_settings.cache_clear()
    yield Settings()
    get_settings.cache_clear()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app

    get_settings.cache_clear()
    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


@pytest.fixture()
def secured(monkeypatch):
    """Enable bearer auth for a single test."""
    from app.config import get_settings

    monkeypatch.setenv("API_BEARER_TOKEN", "s3cret-token")
    get_settings.cache_clear()
    yield "s3cret-token"
    get_settings.cache_clear()
