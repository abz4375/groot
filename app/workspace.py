"""Workspace path resolution.

Every path handed to agy must resolve inside ``WORKSPACE_ROOT``. Symlinks are
followed *before* the containment check so a symlink cannot be used to escape.
"""

from __future__ import annotations

from pathlib import Path

from .config import Settings
from .errors import ApiError


def _check_root(settings: Settings) -> Path:
    root = settings.resolved_ws_root()
    if not root.is_dir():
        raise ApiError(
            503,
            "workspace_missing",
            "WORKSPACE_ROOT does not exist inside the container.",
            {"workspace_root": str(root)},
        )
    return root


def _resolve_within(raw: str, root: Path, *, label: str, must_exist: bool = True) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=must_exist)
    except FileNotFoundError as exc:
        raise ApiError(
            400,
            f"{label}_not_found",
            f"{label} does not exist: {raw}",
            {"path": str(candidate)},
        ) from exc
    except OSError as exc:
        raise ApiError(
            400,
            f"invalid_{label}",
            f"{label} cannot be resolved: {raw}",
            {"reason": str(exc)},
        ) from exc

    if not resolved.is_dir():
        raise ApiError(400, f"{label}_not_a_directory", f"{label} is not a directory: {raw}")

    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ApiError(
            400,
            f"{label}_outside_workspace",
            f"{label} must stay inside WORKSPACE_ROOT.",
            {"workspace_root": str(root), "resolved": str(resolved)},
        ) from exc
    return resolved


def resolve_workdir(value: str | None, settings: Settings) -> Path:
    root = _check_root(settings)
    raw = (value or settings.default_workdir or ".").strip() or "."
    return _resolve_within(raw, root, label="workdir")


def resolve_add_dirs(values: list[str], settings: Settings) -> list[str]:
    if not values:
        return []
    root = _check_root(settings)
    resolved: list[str] = []
    for value in values:
        if not value or not value.strip():
            continue
        resolved.append(str(_resolve_within(value.strip(), root, label="add_dir")))
    return resolved


def resolve_workspace_path(raw: str, settings: Settings) -> Path:
    root = _check_root(settings)
    return _resolve_within(raw, root, label="path")
