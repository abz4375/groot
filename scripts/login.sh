#!/usr/bin/env bash
# Interactive agy login inside the container.
#
# The credential directory is a shared named volume, so logging in once here
# is enough for the API service too — no restart required.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if docker compose ps --status running --services 2>/dev/null | grep -qx "api"; then
    exec docker compose exec -it api agy
fi

echo "api container is not running; starting a one-off container on the same volume."
exec docker compose run --rm api agy
