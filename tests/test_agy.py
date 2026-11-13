"""Unit tests for the agy wrapper, argv construction and path sandboxing."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app import agy
from app.config import Settings
from app.errors import ApiError
from app.workspace import resolve_add_dirs, resolve_workdir

# ---------------------------------------------------------------------------
# argv construction
# ---------------------------------------------------------------------------


def test_argv_minimal(settings: Settings, workspace: Path) -> None:
    run = agy.AgyRun(prompt="hello", workdir=workspace)
    argv = run.to_argv(settings)
    assert argv[0] == settings.agy_bin
    assert argv[1] == "-p"
    assert argv[2] == "hello"
    assert "--output-format" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "--print-timeout" in argv


def test_argv_all_options(settings: Settings, workspace: Path) -> None:
    run = agy.AgyRun(
        prompt="do the thing",
        workdir=workspace,
        output_format="stream-json",
        model="gemini-3-pro",
        effort="high",
        agent="reviewer",
        conversation_id="conv-123",
        json_schema='{"type":"object"}',
        skip_permissions=True,
        timeout_seconds=42,
        add_dirs=("/workspace/other",),
    )
    argv = run.to_argv(settings)
    joined = " ".join(argv)
    assert "--model gemini-3-pro" in joined
    assert "--effort high" in joined
    assert "--agent reviewer" in joined
    assert "--conversation conv-123" in joined
    assert "--json-schema {\"type\":\"object\"}" in joined
    assert "--dangerously-skip-permissions" in joined
    assert "--print-timeout 42s" in joined
    assert "--add-dir /workspace/other" in joined
    assert "--output-format stream-json" in joined


def test_argv_continue_is_exclusive(settings: Settings, workspace: Path) -> None:
    run = agy.AgyRun(prompt="hi", workdir=workspace, continue_conversation=True)
    argv = run.to_argv(settings)
    assert "--continue" in argv
    assert "--conversation" not in argv


def test_prompt_starting_with_dash_is_passed_as_value(settings: Settings, workspace: Path) -> None:
    run = agy.AgyRun(prompt="--not-a-flag", workdir=workspace)
    argv = run.to_argv(settings)
    assert argv[argv.index("-p") + 1] == "--not-a-flag"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_looks_unauthenticated() -> None:
    assert agy.looks_unauthenticated(
        "Error: authentication required. Headless mode uses your cached credentials."
    )
    assert agy.looks_unauthenticated("Please log in to continue")
    assert not agy.looks_unauthenticated("model not found: gemini-9")


def test_parse_listing() -> None:
    raw = """
    Available models:
    gemini-3-pro                Gemini 3 Pro
    gemini-3-flash              Gemini 3 Flash
    gemini-3-pro                Gemini 3 Pro (duplicate)
    """
    entries = agy.parse_listing(raw)
    assert [entry.id for entry in entries] == ["gemini-3-pro", "gemini-3-flash"]
    assert entries[0].label == "Gemini 3 Pro"


def test_strip_ansi() -> None:
    assert agy.strip_ansi("\x1b[31mred\x1b[0m") == "red"


def test_clip_marks_truncation() -> None:
    assert agy.clip("abcdef", 3).startswith("abc")
    assert "truncated" in agy.clip("abcdef", 3)


def test_auth_status_without_token(settings: Settings) -> None:
    status = agy.auth_status(settings)
    assert status.authenticated is False
    assert status.token_present is False
    assert status.hint


def test_auth_status_with_token(tmp_path: Path) -> None:
    settings = Settings(agy_home=tmp_path / "agy")
    token = settings.token_path
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("super-secret", encoding="utf-8")
    status = agy.auth_status(settings)
    assert status.authenticated is True
    assert status.token_modified_at is not None
    assert "super-secret" not in status.model_dump_json()


# ---------------------------------------------------------------------------
# workspace sandboxing
# ---------------------------------------------------------------------------


def test_resolve_workdir_relative(settings: Settings) -> None:
    resolved = resolve_workdir("sub", settings)
    assert resolved == settings.resolved_ws_root() / "sub"


def test_resolve_workdir_default(settings: Settings) -> None:
    assert resolve_workdir(None, settings) == settings.resolved_ws_root()


def test_resolve_workdir_rejects_traversal(settings: Settings) -> None:
    with pytest.raises(ApiError) as exc:
        resolve_workdir("../..", settings)
    assert exc.value.code in {"workdir_outside_workspace", "workdir_not_found"}


def test_resolve_workdir_rejects_missing(settings: Settings) -> None:
    with pytest.raises(ApiError) as exc:
        resolve_workdir("does-not-exist", settings)
    assert exc.value.code == "workdir_not_found"


def test_resolve_workdir_rejects_file(settings: Settings) -> None:
    with pytest.raises(ApiError) as exc:
        resolve_workdir("file.txt", settings)
    assert exc.value.code == "workdir_not_a_directory"


def test_resolve_add_dirs(settings: Settings) -> None:
    assert resolve_add_dirs([], settings) == []
    resolved = resolve_add_dirs(["other"], settings)
    assert resolved == [str(settings.resolved_ws_root() / "other")]
    with pytest.raises(ApiError):
        resolve_add_dirs(["../../etc"], settings)


# ---------------------------------------------------------------------------
# process handling
# ---------------------------------------------------------------------------


async def test_missing_binary_raises_clean_error(tmp_path: Path) -> None:
    settings = Settings(agy_bin="definitely-not-a-real-agy-binary")
    run = agy.AgyRun(prompt="hi", workdir=tmp_path)
    with pytest.raises(ApiError) as exc:
        await agy.run_buffered(run, settings)
    assert exc.value.status_code == 503
    assert exc.value.code == "agy_not_installed"


async def test_terminate_is_idempotent() -> None:
    proc = await asyncio.create_subprocess_exec(
        "python",
        "-c",
        "import time; time.sleep(30)",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        **_posix_session(),
    )
    await agy._terminate(proc, grace=1.0)
    assert proc.returncode is not None
    await agy._terminate(proc, grace=0.1)  # no-op, must not raise


async def test_collect_flags_auth_prompt() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"Authentication required. Please visit the URL to log in:\n")
    reader.feed_data(b"Waiting for authentication (timeout 60s)...\n")
    reader.feed_eof()

    event = asyncio.Event()
    sink: list[str] = []
    await agy._collect(reader, sink, 4096, event)

    assert event.is_set()
    assert "Authentication required" in "".join(sink)


async def test_collect_ignores_normal_output() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"event":"init"}\n')
    reader.feed_eof()

    event = asyncio.Event()
    sink: list[str] = []
    await agy._collect(reader, sink, 4096, event)

    assert not event.is_set()
    assert "".join(sink) == '{"event":"init"}\n'


async def test_collect_truncates_large_streams() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"x" * 5000 + b"\n")
    reader.feed_eof()

    sink: list[str] = []
    await agy._collect(reader, sink, 100)

    assert "truncated" in "".join(sink)


async def test_readline_race_returns_data() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"event":"init"}\n')
    reader.feed_eof()

    kind, raw = await agy._readline_race(reader, asyncio.Event(), 1.0)
    assert kind == agy._DATA
    assert raw == b'{"event":"init"}\n'


async def test_readline_race_reports_eof() -> None:
    reader = asyncio.StreamReader()
    reader.feed_eof()

    kind, _ = await agy._readline_race(reader, asyncio.Event(), 1.0)
    assert kind == agy._EOF


async def test_readline_race_times_out() -> None:
    reader = asyncio.StreamReader()

    kind, _ = await agy._readline_race(reader, asyncio.Event(), 0.01)
    assert kind == agy._TIMEOUT


async def test_readline_race_wakes_on_auth_prompt() -> None:
    reader = asyncio.StreamReader()
    auth = asyncio.Event()
    auth.set()

    kind, _ = await agy._readline_race(reader, auth, 5.0)
    assert kind == agy._AUTH


def _posix_session() -> dict:
    import sys

    return {} if sys.platform == "win32" else {"start_new_session": True}
