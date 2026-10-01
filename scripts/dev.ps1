# Convenience script for local development on Windows.
# Usage: from the project root, run:  .\scripts\dev.ps1

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}
& ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example."
}

# Start the PostgreSQL container when the app is configured to use it.
$dbUrl = (Select-String -Path ".env" -Pattern "^DATABASE_URL=(.*)$").Matches.Groups[1].Value
if ($dbUrl -like "postgresql*") {
    Write-Host "Starting the VeriFi database (docker compose up -d db)..."
    docker compose up -d --wait db
}

# The schema is migrated automatically on startup (Alembic).
& ".venv\Scripts\python.exe" -m uvicorn app.main:app --reload --port 8000
