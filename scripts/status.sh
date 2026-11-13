#!/usr/bin/env bash
# Print the agy credential status reported by the API.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

port="${API_PORT:-8000}"
url="http://127.0.0.1:${port}/v1/auth/status"

if [[ -n "${API_BEARER_TOKEN:-}" ]]; then
    curl -fsS -H "Authorization: Bearer ${API_BEARER_TOKEN}" "$url"
else
    curl -fsS "$url"
fi
echo
