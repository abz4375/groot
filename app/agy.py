"""Thin, well-behaved wrapper around the ``agy`` CLI.

Design notes
------------
* agy is a native Go binary. We never import it — we exec it.
* stdin is always ``DEVNULL`` for one-shot runs. agy has a known bug where
  ``-p`` can block forever waiting on stdin in a non-TTY environment
  (antigravity issue #318); closing stdin removes the ambiguity.
* Every child is started in its own process group so a timeout or a client
  disconnect kills the agent *and* anything it spawned.
* ``--print-timeout`` is passed to agy as a first line of defence, but a
  wall-clock watchdog is enforced here as well.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import sys
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .config import Settings
from .errors import ApiError
from .models import AuthStatus, ModelEntry

OutputFormat = Literal["text", "json", "stream-json"]

_AUTH_HINTS = (
    "authentication required",
    "not authenticated",
    "unauthenticated",
    "please log in",
    "please sign in",
    "no credentials",
    "failed to retrieve token",
    "authentication failed",
    "authentication interrupted",
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
#: "gemini-3-pro              Gemini 3 Pro" -> id + human label
_LISTING_RE = re.compile(r"^(?P<id>[A-Za-z0-9][\w.:/@+-]*)\s{2,}(?P<label>\S.*)$")

_STDOUT_EOF = ""


@dataclass(slots=True)
class AgyRun:
    """A fully-resolved agy invocation."""

    prompt: str
    workdir: Path
    output_format: OutputFormat = "json"
    model: str | None = None
    effort: str | None = None
    agent: str | None = None
    conversation_id: str | None = None
    continue_conversation: bool = False
    json_schema: str | None = None
    skip_permissions: bool = False
    timeout_seconds: int = 900
    add_dirs: tuple[str, ...] = field(default_factory=tuple)

    def to_argv(self, settings: Settings) -> list[str]:
        argv: list[str] = [
            settings.agy_bin,
            "-p",
            self.prompt,
            "--output-format",
            self.output_format,
        ]
        if self.model:
            argv += ["--model", self.model]
        if self.effort:
            argv += ["--effort", self.effort]
        if self.agent:
            argv += ["--agent", self.agent]
        if self.conversation_id:
            argv += ["--conversation", self.conversation_id]
        elif self.continue_conversation:
            argv.append("--continue")
        if self.json_schema:
            argv += ["--json-schema", self.json_schema]
        if self.skip_permissions:
            argv.append("--dangerously-skip-permissions")
        if self.timeout_seconds > 0:
            argv += ["--print-timeout", f"{self.timeout_seconds}s"]
        for extra in self.add_dirs:
            argv += ["--add-dir", extra]
        return argv


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:24]}"


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def looks_unauthenticated(text: str) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in _AUTH_HINTS)


def build_env(settings: Settings) -> dict[str, str]:
    """Environment for agy child processes.

    We deliberately *inherit* the container environment (API keys, proxies)
    and only nudge the knobs that break in a container.
    """
    env = dict(os.environ)
    env.setdefault("AGY_CLI_DISABLE_AUTO_UPDATE", "true")
    env.setdefault("TERM", "dumb")
    env.setdefault("NO_COLOR", "1")
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("CI", "1")
    env.update({str(k): str(v) for k, v in settings.agy_env.items()})
    return env


def dependency_status(settings: Settings) -> tuple[bool, str | None]:
    """Is the agy binary runnable?"""
    candidate = settings.agy_bin
    if os.path.sep in candidate or (os.path.altsep and os.path.altsep in candidate):
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return True, str(path)
        return False, None
    found = shutil.which(candidate, path=build_env(settings).get("PATH"))
    return (found is not None), found


#: Windows has no SIGKILL; there ``kill()`` is the hard kill and ``terminate()``
#: the polite one.
_SIGKILL = getattr(signal, "SIGKILL", None)


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    if sys.platform == "win32":
        if _SIGKILL is not None and sig == _SIGKILL:
            proc.kill()
        else:
            proc.terminate()
        return
    os.killpg(os.getpgid(proc.pid), sig)


async def _terminate(proc: asyncio.subprocess.Process, grace: float = 10.0) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(OSError):
        _signal_group(proc, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
        return
    except TimeoutError:
        pass
    with contextlib.suppress(OSError):
        _signal_group(proc, _SIGKILL if _SIGKILL is not None else signal.SIGTERM)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=5)


async def _spawn(run: AgyRun, settings: Settings) -> asyncio.subprocess.Process:
    argv = run.to_argv(settings)
    kwargs: dict[str, Any] = {
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "cwd": str(run.workdir),
        "env": build_env(settings),
        "limit": settings.max_line_bytes,
    }
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    try:
        return await asyncio.create_subprocess_exec(*argv, **kwargs)
    except FileNotFoundError as exc:
        raise ApiError(
            503,
            "agy_not_installed",
            "The agy CLI is not available inside this container.",
            {"agy_bin": settings.agy_bin},
        ) from exc
    except NotADirectoryError as exc:
        raise ApiError(
            400,
            "invalid_workdir",
            "Working directory does not exist.",
            {"cwd": str(run.workdir)},
        ) from exc
    except PermissionError as exc:
        raise ApiError(
            503,
            "agy_not_executable",
            "The agy binary is not executable.",
            {"agy_bin": settings.agy_bin},
        ) from exc


async def _collect(
    stream: asyncio.StreamReader | None,
    sink: list[str],
    max_bytes: int,
    auth_event: asyncio.Event | None = None,
) -> None:
    """Read a pipe to EOF, keeping at most ``max_bytes`` of it.

    When ``auth_event`` is given it is set as soon as agy's interactive login
    prompt appears, which lets callers fail fast with a 401 instead of waiting
    for agy's own 60-second authentication timeout.
    """
    if stream is None:
        return
    total = 0
    while True:
        try:
            raw = await stream.readline()
        except (ValueError, asyncio.LimitOverrunError):
            break
        if not raw:
            break
        text = raw.decode("utf-8", "replace")
        if total < max_bytes:
            sink.append(text)
            total += len(raw)
        if auth_event is not None and not auth_event.is_set() and looks_unauthenticated(text):
            auth_event.set()
    if total >= max_bytes:
        sink.append(f"\n... [truncated at {max_bytes} bytes]")


async def _drain_stderr(
    stream: asyncio.StreamReader | None,
    sink: list[str],
    max_bytes: int,
    auth_event: asyncio.Event | None = None,
) -> None:
    await _collect(stream, sink, max_bytes, auth_event)


async def _drain_quietly(stream: asyncio.StreamReader | None, budget_bytes: int = 1_000_000) -> None:
    if stream is None:
        return
    consumed = 0
    while consumed < budget_bytes:
        try:
            raw = await stream.readline()
        except (ValueError, asyncio.LimitOverrunError):
            return
        if not raw:
            return
        consumed += len(raw)


_EOF = "eof"
_DATA = "data"
_TIMEOUT = "timeout"
_AUTH = "auth"
_LINE_ERROR = "error"


async def _readline_race(
    stream: asyncio.StreamReader,
    auth_event: asyncio.Event,
    timeout: float,
) -> tuple[str, bytes | None]:
    """Read one stdout line, but wake up early if the auth prompt appears.

    Plain ``wait_for(readline, timeout)`` would make an unauthenticated caller
    wait a full heartbeat (and agy 60 s) before the 401 surfaces.
    """
    read_task = asyncio.create_task(stream.readline())
    auth_task = asyncio.create_task(auth_event.wait())
    try:
        done, _pending = await asyncio.wait(
            {read_task, auth_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        auth_task.cancel()

    if read_task in done:
        try:
            raw = read_task.result()
        except (ValueError, asyncio.LimitOverrunError):
            return _LINE_ERROR, None
        return (_EOF if raw == b"" else _DATA), raw

    if not done:
        read_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await read_task
        return _TIMEOUT, None

    # Auth prompt won; stdout is silent while agy waits for a code.
    read_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await read_task
    return _AUTH, None


# ---------------------------------------------------------------------------
# one-shot execution
# ---------------------------------------------------------------------------


def _parse_json_payload(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        return payload
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            return candidate
    return None


async def run_buffered(
    run: AgyRun,
    settings: Settings,
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run agy to completion and return a single JSON-friendly result.

    stdout and stderr are consumed concurrently. stderr is watched for agy's
    interactive login prompt so an unauthenticated caller gets a 401 in about a
    second instead of waiting out agy's 60-second auth timeout.
    """
    run_id = new_run_id()
    started = time.monotonic()
    proc = await _spawn(run, settings)

    out_chunks: list[str] = []
    err_chunks: list[str] = []
    auth_seen = asyncio.Event()

    out_task = asyncio.create_task(_collect(proc.stdout, out_chunks, settings.max_stdout_bytes))
    err_task = asyncio.create_task(
        _collect(proc.stderr, err_chunks, settings.max_stderr_bytes, auth_seen)
    )
    wait_task = asyncio.create_task(proc.wait())
    auth_task = asyncio.create_task(auth_seen.wait())

    timed_out = False
    try:
        done, _pending = await asyncio.wait(
            {wait_task, auth_task},
            timeout=run.timeout_seconds + settings.timeout_grace_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            timed_out = True
            await _terminate(proc)
        elif auth_task in done:
            await _terminate(proc)
    finally:
        auth_task.cancel()
        if proc.returncode is None:
            await _terminate(proc)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(out_task, err_task, wait_task, return_exceptions=True),
                timeout=10,
            )

    duration = round(time.monotonic() - started, 3)
    raw_text = "".join(out_chunks)
    stdout = strip_ansi(raw_text) if settings.strip_ansi else raw_text
    stderr = "".join(err_chunks)
    exit_code = proc.returncode if proc.returncode is not None else -1

    if auth_seen.is_set() or (exit_code != 0 and looks_unauthenticated(stderr)):
        raise ApiError(
            401,
            "agy_not_authenticated",
            "agy has no usable credentials. Log in once inside the container.",
            {"run_id": run_id, "stderr": clip(stderr, 2000)},
        )
    if timed_out:
        raise ApiError(
            504,
            "agy_timeout",
            "agy did not finish within the allotted time.",
            {"timeout_seconds": run.timeout_seconds, "run_id": run_id},
        )

    envelope = _parse_json_payload(stdout) if run.output_format == "json" else None
    if envelope is None:
        status = "SUCCESS" if exit_code == 0 else "ERROR"
        response_text = stdout.strip()
    else:
        status = str(envelope.get("status") or ("SUCCESS" if exit_code == 0 else "ERROR"))
        response_text = envelope.get("response")
        if response_text is None:
            response_text = envelope.get("error") or ""

    result: dict[str, Any] = {
        "run_id": run_id,
        "ok": exit_code == 0 and status.upper() == "SUCCESS",
        "status": status.upper(),
        "exit_code": exit_code,
        "response": response_text,
        "error": (envelope or {}).get("error"),
        "conversation_id": (envelope or {}).get("conversation_id"),
        "structured_output": (envelope or {}).get("structured_output"),
        "usage": (envelope or {}).get("usage"),
        "num_turns": (envelope or {}).get("num_turns"),
        "duration_seconds": duration,
        "agy_duration_seconds": (envelope or {}).get("duration_seconds"),
        "stderr": clip(stderr, 8000) or None,
        "metadata": metadata,
    }
    return result


# ---------------------------------------------------------------------------
# streaming execution
# ---------------------------------------------------------------------------


async def iter_stream(
    run: AgyRun,
    settings: Settings,
    *,
    metadata: dict[str, Any] | None = None,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Yield ``(event_name, payload)`` tuples from an agy stream-json run.

    Synthetic events added by this server: ``run`` (first), ``keepalive``,
    ``log``, ``stderr``, ``error`` and ``end`` (last). Everything else is a
    verbatim agy event: ``init``, ``step_update``, ``result``.
    """
    run_id = new_run_id()
    started = time.monotonic()
    yield (
        "run",
        {
            "run_id": run_id,
            "started_at": utc_now_iso(),
            "workdir": str(run.workdir),
            "model": run.model,
            "agent": run.agent,
            "conversation_id": run.conversation_id,
            "timeout_seconds": run.timeout_seconds,
            "metadata": metadata,
        },
    )

    stderr_lines: list[str] = []
    auth_seen = asyncio.Event()
    exit_code: int | None = None
    timed_out = False
    auth_failed = False
    result_status: str | None = None

    proc = await _spawn(run, settings)
    stderr_task = asyncio.create_task(
        _drain_stderr(proc.stderr, stderr_lines, settings.max_stderr_bytes, auth_seen)
    )
    deadline = started + run.timeout_seconds + settings.timeout_grace_seconds

    try:
        while True:
            # agy waits up to 60s for an interactive code when unauthenticated.
            # The stderr reader flags that prompt and `_readline_race` returns
            # immediately, so the caller sees a 401 instead of a 60s hang.
            if auth_seen.is_set():
                auth_failed = True
                yield (
                    "error",
                    {
                        "code": "agy_not_authenticated",
                        "message": "agy has no usable credentials. Log in once inside the container.",
                        "run_id": run_id,
                    },
                )
                break
            if time.monotonic() > deadline:
                timed_out = True
                yield (
                    "error",
                    {
                        "code": "agy_timeout",
                        "message": "agy did not finish within the allotted time.",
                        "timeout_seconds": run.timeout_seconds,
                        "run_id": run_id,
                    },
                )
                break

            kind, raw = await _readline_race(proc.stdout, auth_seen, settings.heartbeat_seconds)
            if kind == _TIMEOUT:
                yield ("keepalive", {"ts": utc_now_iso()})
                continue
            if kind == _AUTH:
                auth_failed = True
                yield (
                    "error",
                    {
                        "code": "agy_not_authenticated",
                        "message": "agy has no usable credentials. Log in once inside the container.",
                        "run_id": run_id,
                    },
                )
                break
            if kind == _LINE_ERROR:
                yield ("log", {"stream": "stdout", "line": "[line exceeded buffer limit; skipped]"})
                continue
            if kind == _EOF or not raw:
                break

            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if settings.strip_ansi:
                line = strip_ansi(line)
            if not line.strip():
                continue

            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                yield ("log", {"stream": "stdout", "line": clip(line, 4000)})
                continue
            if not isinstance(payload, dict):
                yield ("log", {"stream": "stdout", "line": clip(line, 4000)})
                continue

            event = str(payload.get("event") or "message")
            if event == "result":
                result_status = str(payload.get("status") or "")
            yield (event, payload)
            if event == "result":
                # Do not stop reading yet: draining the pipe lets agy exit
                # cleanly (and flush its conversation state) instead of
                # blocking on a full buffer.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        _drain_quietly(proc.stdout),
                        timeout=settings.timeout_grace_seconds,
                    )
                break

        if proc.returncode is None:
            if timed_out or auth_failed:
                await _terminate(proc)
            else:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=min(settings.timeout_grace_seconds, 15.0))
                except TimeoutError:
                    await _terminate(proc)
        exit_code = proc.returncode if proc.returncode is not None else -1
    finally:
        if proc.returncode is None:
            await _terminate(proc)
        with contextlib.suppress(asyncio.CancelledError):
            try:
                await asyncio.wait_for(stderr_task, timeout=5)
            except TimeoutError:
                stderr_task.cancel()
            except asyncio.CancelledError:
                stderr_task.cancel()

    stderr_text = clip("".join(stderr_lines).strip(), 8000)
    failed = exit_code not in (0, None)
    if not timed_out and not auth_failed and failed and looks_unauthenticated(stderr_text):
        yield (
            "error",
            {
                "code": "agy_not_authenticated",
                "message": "agy has no usable credentials. Log in once inside the container.",
                "run_id": run_id,
            },
        )
    elif not timed_out and not auth_failed and failed:
        yield (
            "error",
            {
                "code": "agy_run_failed",
                "message": "agy exited with a non-zero status.",
                "exit_code": exit_code,
                "run_id": run_id,
            },
        )

    if stderr_text:
        yield ("stderr", {"text": stderr_text})

    yield (
        "end",
        {
            "run_id": run_id,
            "exit_code": exit_code,
            "status": result_status or ("ERROR" if exit_code not in (0, None) else "SUCCESS"),
            "duration_seconds": round(time.monotonic() - started, 3),
        },
    )


# ---------------------------------------------------------------------------
# auth + introspection
# ---------------------------------------------------------------------------


def auth_status(settings: Settings) -> AuthStatus:
    home = settings.agy_home
    token = settings.token_path
    token_present = token.is_file()
    modified_at: str | None = None
    age: int | None = None
    if token_present:
        stat = token.stat()
        modified_at = datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(timespec="seconds")
        age = int(time.time() - stat.st_mtime)

    files: list[str] = []
    if home.is_dir():
        with contextlib.suppress(OSError):
            files = sorted(entry.name for entry in home.iterdir() if entry.is_file() or entry.is_dir())

    hint = None
    if not token_present:
        hint = (
            "No credential file found. Run an interactive login once: "
            "`docker compose exec -it api agy` (or `./scripts/login.sh`), then retry."
        )

    return AuthStatus(
        authenticated=token_present,
        token_present=token_present,
        token_path=str(token),
        token_modified_at=modified_at,
        token_age_seconds=age,
        agy_home=str(home),
        agy_home_exists=home.is_dir(),
        files=files,
        hint=hint,
    )


_version_cache: tuple[float, str | None] | None = None
_listing_cache: dict[str, tuple[float, list[ModelEntry], str]] = {}
_cache_lock = asyncio.Lock()


async def _run_simple(args: list[str], settings: Settings, timeout: float = 45.0) -> tuple[int, str, str]:
    argv = [settings.agy_bin, *args]
    kwargs: dict[str, Any] = {
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "env": build_env(settings),
        "cwd": str(settings.resolved_ws_root() if settings.resolved_ws_root().is_dir() else Path.cwd()),
        "limit": settings.max_line_bytes,
    }
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    try:
        proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
    except FileNotFoundError as exc:
        raise ApiError(
            503,
            "agy_not_installed",
            "The agy CLI is not available inside this container.",
        ) from exc
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as exc:
        await _terminate(proc)
        raise ApiError(504, "agy_timeout", f"`agy {' '.join(args)}` timed out after {timeout}s.") from exc
    text = stdout.decode("utf-8", "replace")
    err = stderr.decode("utf-8", "replace")
    if settings.strip_ansi:
        text = strip_ansi(text)
        err = strip_ansi(err)
    return (proc.returncode or 0), text, err


async def probe_version(settings: Settings, *, refresh: bool = False) -> str | None:
    global _version_cache
    now = time.monotonic()
    if not refresh and _version_cache and now - _version_cache[0] < settings.models_cache_ttl_seconds:
        return _version_cache[1]
    try:
        _, out, err = await _run_simple(["--version"], settings, timeout=20)
        text = (out or err).strip().splitlines()
        version = text[0].strip() if text else None
    except ApiError:
        version = None
    _version_cache = (now, version)
    return version


def parse_listing(raw: str) -> list[ModelEntry]:
    entries: list[ModelEntry] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        line = line.rstrip()
        if not line.strip() or line.lstrip().startswith(("#", "-", "*")):
            continue
        match = _LISTING_RE.match(line.strip())
        if not match:
            continue
        identifier = match.group("id")
        if identifier in seen:
            continue
        seen.add(identifier)
        entries.append(ModelEntry(id=identifier, label=match.group("label").strip()))
    return entries


async def _cached_listing(
    kind: str,
    command: list[str],
    settings: Settings,
) -> tuple[list[ModelEntry], bool, str]:
    now = time.monotonic()
    async with _cache_lock:
        hit = _listing_cache.get(kind)
        if hit and now - hit[0] < settings.models_cache_ttl_seconds:
            return hit[1], True, hit[2]
        exit_code, out, err = await _run_simple(command, settings)
        raw = out.strip()
        if exit_code != 0 and looks_unauthenticated(err + raw):
            raise ApiError(
                401,
                "agy_not_authenticated",
                "agy has no usable credentials. Log in once inside the container.",
                {"stderr": clip(err, 2000)},
            )
        if exit_code != 0:
            raise ApiError(
                502,
                "agy_command_failed",
                f"`agy {' '.join(command)}` failed.",
                {"exit_code": exit_code, "stderr": clip(err, 2000)},
            )
        entries = parse_listing(raw)
        _listing_cache[kind] = (now, entries, clip(raw, 20_000))
        return entries, False, clip(raw, 20_000)


async def list_models(settings: Settings) -> tuple[list[ModelEntry], bool, str]:
    return await _cached_listing("models", ["models"], settings)


async def list_agents(settings: Settings) -> tuple[list[ModelEntry], bool, str]:
    return await _cached_listing("agents", ["agents"], settings)


def clear_caches() -> None:
    global _version_cache
    _version_cache = None
    _listing_cache.clear()
