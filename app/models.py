"""Request/response schemas."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

StreamFormat = Literal["sse", "ndjson"]


class RunRequest(BaseModel):
    """A single headless agy invocation."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(
        min_length=1,
        description="Prompt passed to `agy -p`. This is the user turn of the conversation.",
    )
    model: str | None = Field(
        default=None,
        description="Model slug, e.g. `gemini-3-pro`. Run `GET /v1/models` to list available slugs.",
    )
    effort: str | None = Field(default=None, description="Reasoning effort, e.g. `low`, `medium`, `high`.")
    agent: str | None = Field(default=None, description="Named agent to run the prompt with.")
    conversation_id: str | None = Field(
        default=None,
        description="Resume a specific conversation by id (mutually exclusive with `continue_conversation`).",
    )
    continue_conversation: bool = Field(
        default=False,
        description="Continue the latest conversation in `workdir` (exclusive with `conversation_id`).",
    )
    json_schema: dict[str, Any] | str | None = Field(
        default=None,
        description="JSON Schema (object or JSON-encoded string) that constrains the final response.",
    )
    skip_permissions: bool = Field(
        default=False,
        description="Pass `--dangerously-skip-permissions`. Only enable in a disposable sandbox.",
    )
    workdir: str | None = Field(
        default=None,
        description="Absolute, or relative to `WORKSPACE_ROOT`. Must stay inside the workspace.",
    )
    add_dirs: list[str] = Field(
        default_factory=list,
        description="Extra directories the agent may read. Must stay inside `WORKSPACE_ROOT`.",
    )
    timeout_seconds: int | None = Field(
        default=None,
        ge=1,
        description="Wall-clock budget. Defaults to `DEFAULT_TIMEOUT_SECONDS`, max `MAX_TIMEOUT_SECONDS`.",
    )
    output_format: Literal["json", "text"] = Field(
        default="json",
        description="Buffered response format. Streaming endpoints always use `stream-json`.",
    )
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="Opaque caller metadata echoed back on the response. Not passed to agy.",
    )

    @field_validator("effort")
    @classmethod
    def _clean_effort(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if not value:
            return None
        if not all(ch.isalnum() or ch in "-_" for ch in value):
            raise ValueError("effort must be alphanumeric (dashes/underscores allowed)")
        return value

    @field_validator("model", "agent", "conversation_id")
    @classmethod
    def _clean_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def _exclusive_conversation(self) -> RunRequest:
        if self.conversation_id and self.continue_conversation:
            raise ValueError("conversation_id and continue_conversation are mutually exclusive")
        return self

    def resolved_json_schema(self) -> str | None:
        """Normalise the schema to the string form agy expects."""
        if self.json_schema is None:
            return None
        if isinstance(self.json_schema, str):
            text = self.json_schema.strip()
            if not text:
                return None
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"json_schema is not valid JSON: {exc.msg}") from exc
            if not isinstance(parsed, dict):
                raise ValueError("json_schema must decode to a JSON object")
            return json.dumps(parsed, separators=(",", ":"))
        return json.dumps(self.json_schema, separators=(",", ":"))


class RunResult(BaseModel):
    run_id: str
    ok: bool
    status: str
    exit_code: int
    response: str | None = None
    error: str | None = None
    conversation_id: str | None = None
    structured_output: Any | None = None
    usage: dict[str, Any] | None = None
    num_turns: int | None = None
    duration_seconds: float
    agy_duration_seconds: float | None = None
    stderr: str | None = None
    metadata: dict[str, Any] | None = None


class AuthStatus(BaseModel):
    authenticated: bool
    token_present: bool
    token_path: str
    token_modified_at: str | None = None
    token_age_seconds: int | None = None
    agy_home: str
    agy_home_exists: bool
    files: list[str] = Field(default_factory=list)
    hint: str | None = None


class ModelEntry(BaseModel):
    id: str
    label: str | None = None


class ModelsResponse(BaseModel):
    models: list[ModelEntry]
    cached: bool = False
    raw: str | None = None


class AgentsResponse(BaseModel):
    agents: list[ModelEntry]
    cached: bool = False
    raw: str | None = None


class InfoResponse(BaseModel):
    service: str
    version: str
    agy_bin: str
    agy_version: str | None = None
    workspace_root: str
    default_workdir: str
    max_concurrency: int
    auth_required: bool
    authenticated: bool
    keyring_expected: bool
