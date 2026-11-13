# Interactive agy login inside the container (Windows / PowerShell).
#
# The credential directory is a shared named volume, so logging in once here
# is enough for the API service too — no restart required.
#Requires -Version 5.1
$ErrorActionPreference = "Stop"

Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    $services = docker compose ps --status running --services
    if ($services -contains "api") {
        docker compose exec -it api agy
    }
    else {
        Write-Host "api container is not running; starting a one-off container on the same volume."
        docker compose run --rm api agy
    }
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
