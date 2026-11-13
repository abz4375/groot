# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# ---------------------------------------------------------------------------
# Stage 1 — install the Google Antigravity CLI (agy)
#
# agy ships as a signed native binary, not as a pip/npm package, so the only
# supported installation path is the official installer. We install into a
# scratch stage and copy the single binary forward, keeping the runtime image
# free of the installer's build dependencies.
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS agy-installer

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV DEBIAN_FRONTEND=noninteractive \
    AGY_CLI_DISABLE_AUTO_UPDATE=true

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl bash coreutils \
    && rm -rf /var/lib/apt/lists/*

# The installer verifies the SHA512 manifest and writes the binary to
# $HOME/.local/bin by default. We point it straight at /usr/local/bin so the
# runtime stage can copy one file.
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash -s -- --dir /usr/local/bin

RUN set -eux; \
    test -x /usr/local/bin/agy; \
    HOME=/tmp timeout 60 /usr/local/bin/agy --version 2>&1 | head -n 5 || true

# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # agy stores everything under $HOME/.gemini/antigravity-cli
    HOME=/home/app \
    # Never let the CLI self-update inside a container
    AGY_CLI_DISABLE_AUTO_UPDATE=true \
    WORKSPACE_ROOT=/workspace

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        ripgrep \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/app --shell /bin/bash app

COPY --from=agy-installer /usr/local/bin/agy /usr/local/bin/agy

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY pyproject.toml ./

# Create the credential directory *in the image* and own it as `app`, so
# Docker seeds the named volume with the right ownership on first use.
RUN mkdir -p /workspace /home/app/.gemini/antigravity-cli \
    && chown -R app:app /home/app /workspace

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# tini reaps the agy process group when the container is stopped
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
