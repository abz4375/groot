# groot

A Dockerised HTTP API around the **Google Antigravity CLI (`agy`)**. The image
contains the real `agy` binary; the API is a thin, well-behaved wrapper that
turns headless `agy` invocations into JSON and streaming endpoints.

- **FastAPI** (Python 3.12), non-root container, `tini` as PID 1.
- `agy` installed via the official installer, copied into the runtime image.
- **Credentials persist across container restarts** through a named volume at
  `$HOME/.gemini/antigravity-cli`.
- Login is deliberately manual and interactive — one `docker compose exec` and
  you are done.

---

## 1. Quick start

```bash
cp .env.example .env          # optional, but set API_BEARER_TOKEN here
docker compose build
docker compose up -d
```

Log in once (this is the only interactive step):

```bash
./scripts/login.sh            # Windows: .\scripts\login.ps1
# or directly:
docker compose exec -it api agy
```

`agy` will print an authorisation URL (and ask for a code), because the
container has no browser. Complete it on your host machine, paste the code
back, and quit the TUI. Check it worked:

```bash
./scripts/status.sh
# {"authenticated": true, "token_present": true, "token_path": "...", ...}
```

Run a prompt:

```bash
curl -s http://localhost:8000/v1/runs \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "Explain what this repository does in two sentences."}'
```

---

## 2. Configuration

Everything is environment-driven (`.env` is read by both Compose and the app).

| Variable | Default | Meaning |
| --- | --- | --- |
| `API_PORT` | `8000` | Host port to publish. |
| `API_BEARER_TOKEN` | *empty* | If set, every `/v1` endpoint requires `Authorization: Bearer <token>`. Empty = open API. |
| `CORS_ORIGINS` | *empty* | Comma-separated allow-list; empty disables CORS middleware. |
| `WORKSPACE_ROOT` | `/workspace` | Sandbox root. All `workdir` values must resolve inside it. |
| `DEFAULT_WORKDIR` | `.` | Workdir used when a request omits `workdir`. |
| `MAX_CONCURRENCY` | `2` | Maximum simultaneous `agy` processes. |
| `QUEUE_WAIT_SECONDS` | `1.0` | How long a request waits for a free slot before a `429`. |
| `DEFAULT_TIMEOUT_SECONDS` | `900` | Per-run wall clock when the request omits a timeout. |
| `MAX_TIMEOUT_SECONDS` | `3600` | Hard ceiling for a requested timeout. |
| `MAX_PROMPT_CHARS` | `200000` | Prompt size limit (`413` beyond this). |
| `AGY_BIN` | `agy` | Path/name of the CLI. |
| `AGY_HOME` | `$HOME/.gemini/antigravity-cli` | Credential directory. |
| `AGY_ENV` | `{}` | JSON object of extra env vars for every `agy` process. |
| `LOG_LEVEL` | `INFO` | Standard logging level. |

Anything `agy` itself reads (`GEMINI_API_KEY`, proxies, `AGY_ADC_AUTH`, …) is
inherited from the container environment, so you can add it to `.env` and it
reaches the CLI unchanged.

---

## 3. API

All `/v1` routes require the bearer token when `API_BEARER_TOKEN` is set.
Errors use one envelope everywhere:

```json
{"error": {"code": "agy_not_authenticated", "message": "...", "details": {}, "request_id": "..."}}
```

### Probes

| Route | Purpose |
| --- | --- |
| `GET /healthz` | Liveness. Always `200` while the process is up. |
| `GET /readyz` | Readiness. `503` until the binary exists, the workspace mounts **and** credentials are present. |

Compose uses `/healthz` so the container is not killed before you have logged
in; `/readyz` is the gate for load balancers.

### Introspection

| Route | Purpose |
| --- | --- |
| `GET /v1/info` | Versions and effective configuration. |
| `GET /v1/auth/status` | Is agy logged in? Never returns token contents. |
| `GET /v1/models` | Parsed `agy models` output (cached, `MODELS_CACHE_TTL_SECONDS`). |
| `GET /v1/agents` | Parsed `agy agents` output (cached). |

### `POST /v1/runs` — buffered

Runs `agy -p "<prompt>" --output-format json` and returns the final envelope.

```bash
curl -s http://localhost:8000/v1/runs \
  -H "Authorization: Bearer $API_BEARER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "prompt": "Summarise src/ and list the riskiest module.",
    "model": "gemini-3-pro",
    "workdir": "my-project",
    "timeout_seconds": 300,
    "json_schema": {
      "type": "object",
      "properties": {"summary": {"type": "string"}, "risk": {"type": "string"}},
      "required": ["summary", "risk"]
    }
  }'
```

```json
{
  "run_id": "run_8f2c...",
  "ok": true,
  "status": "SUCCESS",
  "exit_code": 0,
  "response": "This repository ...",
  "conversation_id": "…",
  "structured_output": {"summary": "...", "risk": "..."},
  "usage": {"input_tokens": 1200, "output_tokens": 310},
  "num_turns": 2,
  "duration_seconds": 12.4,
  "agy_duration_seconds": 11.9,
  "stderr": null,
  "metadata": null
}
```

Request fields: `prompt` (required), `model`, `effort`, `agent`,
`conversation_id`, `continue_conversation`, `json_schema`, `skip_permissions`,
`workdir`, `add_dirs`, `timeout_seconds`, `output_format` (`json`|`text`),
`metadata` (echoed back, never sent to agy).

> **Status semantics.** A run that agy completes — even with `status: "ERROR"`
> — returns HTTP `200` with `ok: false` and the error in `error`. HTTP status
> codes are reserved for transport/infrastructure problems (`401`, `404`,
> `413`, `429`, `503`, `504`). This keeps buffered and streaming clients on the
> same mental model.

### `POST /v1/runs/stream` — streaming

Same body, but always runs agy with `--output-format stream-json`.

- `?format=sse` (default) → `text/event-stream`
- `?format=ndjson` → `application/x-ndjson`

```bash
curl -N http://localhost:8000/v1/runs/stream \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "Refactor utils.py and run the tests."}'
```

```
id: 1
event: run
data: {"run_id":"run_8f2c...","workdir":"/workspace","model":null,...}

id: 2
event: init
data: {"event":"init","init":{...}}

id: 3
event: step_update
data: {"event":"step_update","step_index":0,"state":"ACTIVE","step_type":"agent_response","text_delta":"Let me"}

id: 4
event: result
data: {"event":"result","status":"SUCCESS","response":"...","conversation_id":"..."}

id: 5
event: end
data: {"run_id":"run_8f2c...","exit_code":0,"status":"SUCCESS","duration_seconds":18.2}
```

Event names: `run` (synthetic, first), `init`, `step_update`, `result`
(verbatim agy events), `keepalive`, `log`, `stderr`, `error`, and `end`
(synthetic, last). NDJSON frames are `{"seq", "event", "payload"}`.

Disconnecting the client terminates the whole `agy` process group — nothing is
left running.

---

## 4. How the login stays persisted

`agy` stores its profile under `$HOME/.gemini/antigravity-cli`:

```
settings.json
antigravity-oauth-token      ← written when no OS keyring is available
log/
updater/
conversations/
```

The container has **no D-Bus / Secret Service**, so on start-up agy logs
`keyring failed, falling back to file storage` and uses that token file. That
is exactly what we want: a plain file inside a volume.

Compose mounts the whole directory as a named volume:

```yaml
volumes:
  - agy_auth:/home/app/.gemini/antigravity-cli
```

Two things matter here:

1. **Mount the directory, never individual files.** agy writes
   `settings.json` with write-temp-then-`rename`. A file-level bind mount pins
   the inode, and the rename fails with `Device or resource busy`.
2. **Ownership.** The Dockerfile creates the directory as `app` (uid 10001)
   before `USER app`, so Docker seeds the volume with the correct owner on
   first use. If you switch to a bind mount, `chown -R 10001:10001 ./agy-auth`
   first.

The self-updater is disabled with `AGY_CLI_DISABLE_AUTO_UPDATE=true`, because
inside a read-mostly container it can hang on `updater/update.lock`.

Credentials survive `docker compose down` / `up`. To wipe them:

```bash
docker compose down -v          # removes groot_agy_auth too
```

### Alternative: Application Default Credentials

If you would rather not do the interactive dance, mount a service-account
file and set:

```
AGY_ADC_AUTH=true
GOOGLE_APPLICATION_CREDENTIALS=/secrets/adc.json
```

Add the bind mount in `docker-compose.yml`. Every run inherits the env vars.

---

## 5. Repository layout

```
app/
  main.py       FastAPI factory, pure-ASGI request-id middleware, error handlers
  routes.py     HTTP surface (probes, introspection, buffered + streaming runs)
  agy.py        argv builder, process supervision, NDJSON parsing, auth probing
  models.py     Pydantic request/response schemas
  workspace.py  path sandboxing (symlink-safe containment in WORKSPACE_ROOT)
  auth.py       bearer-token dependency
  config.py     pydantic-settings
  errors.py     ApiError + error envelope
scripts/
  login.sh, login.ps1   interactive agy login against the shared volume
  status.sh             print /v1/auth/status
tests/                  pytest + FastAPI TestClient, no network, no real agy
Dockerfile              two-stage: installer → runtime
docker-compose.yml      volume wiring + healthcheck
```

---

## 6. Security notes

- Runs as uid `10001`, not root; `tini` reaps orphaned agent subprocesses.
- Only the container's own filesystem is reachable. `workdir` / `add_dirs` are
  resolved with `Path.resolve()` **then** checked against `WORKSPACE_ROOT`, so
  `../` and symlink escapes are rejected with `400`.
- `--dangerously-skip-permissions` is exposed as `skip_permissions` but off by
  default. Enable it only in a throwaway sandbox: the agent can then edit any
  file in the workspace and run shell commands.
- Set `API_BEARER_TOKEN` before exposing the port beyond localhost. Startup
  logs a warning when it is empty.
- Prompts are passed as argv, not through a shell — no shell injection surface.
- The image deliberately omits `dbus`/`gnome-keyring` so agy always uses file
  credentials rather than trying (and failing) to talk to a keyring.

---

## 7. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `503 agy_not_installed` | The binary is missing or not on `PATH`. Rebuild: `docker compose build --no-cache`. |
| `401 agy_not_authenticated` | No credentials yet, or the volume is empty. Run `./scripts/login.sh`. |
| `Device or resource busy` on `settings.json` | You bind-mounted a *file* instead of the directory. Mount `~/.gemini/antigravity-cli`. |
| `/readyz` returns 503 but runs work | Expected until credentials exist; it is the readiness gate, not liveness. |
| Run exceeds the timeout | `504` from the API and the process group is killed. Raise `timeout_seconds` (≤ `MAX_TIMEOUT_SECONDS`). |
| `429 server_busy` | `MAX_CONCURRENCY` slots are saturated. Raise it or retry — `Retry-After: 5` is set. |
| `agy -p` hangs in a script | Known upstream issue in non-TTY environments. The wrapper always closes stdin, passes `--print-timeout`, and enforces its own watchdog, so it cannot hang forever. |
| Permission denied writing to `/workspace` on Linux | Host bind mount ownership. `sudo chown -R 10001:10001 ./workspace`. |

---

## 8. Development

```bash
python -m venv .venv
. .venv/bin/activate           # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
ruff check app tests
pytest -q
uvicorn app.main:app --reload --port 8000
```

Tests never invoke a real `agy` process by default (except one assertion that a
missing binary produces a clean `503`), so they run anywhere.
