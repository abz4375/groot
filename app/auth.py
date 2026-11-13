"""Bearer-token authentication for the HTTP API."""

from __future__ import annotations

import secrets

from fastapi import Header

from .config import get_settings
from .errors import ApiError


async def require_token(authorization: str | None = Header(default=None)) -> None:
    """Enforce ``Authorization: Bearer <API_BEARER_TOKEN>`` when configured.

    If ``API_BEARER_TOKEN`` is empty the API is open — acceptable on a private
    docker network, dangerous anywhere else. Startup logs a warning.
    """
    expected = get_settings().api_bearer_token.strip()
    if not expected:
        return
    if not authorization:
        raise ApiError(
            401,
            "unauthorized",
            "Missing Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, _, supplied = authorization.partition(" ")
    if scheme.lower() != "bearer" or not supplied.strip():
        raise ApiError(
            401,
            "unauthorized",
            "Expected an `Authorization: Bearer <token>` header.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not secrets.compare_digest(supplied.strip(), expected):
        raise ApiError(401, "unauthorized", "Invalid bearer token.", headers={"WWW-Authenticate": "Bearer"})
