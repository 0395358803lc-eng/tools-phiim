$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location -LiteralPath $projectRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Missing .venv\Scripts\python.exe" }

& $python -m pip check
if ($LASTEXITCODE -ne 0) { throw "pip check failed" }
& $python -m ruff check .
if ($LASTEXITCODE -ne 0) { throw "ruff failed" }
node --check static\app.js
if ($LASTEXITCODE -ne 0) { throw "JavaScript syntax check failed" }
& $python -m pytest --cov=flow_story_studio --cov-report=term-missing --cov-report=xml
if ($LASTEXITCODE -ne 0) { throw "pytest/coverage failed" }
& $python -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw "Playwright Chromium install failed" }
& $python scripts\browser-e2e.py
if ($LASTEXITCODE -ne 0) { throw "Browser E2E failed" }
& $python -m bandit -q -r src\flow_story_studio
if ($LASTEXITCODE -ne 0) { throw "bandit failed" }
& $python -m pip_audit --skip-editable
if ($LASTEXITCODE -ne 0) { throw "pip-audit failed" }
& $python -m pip_audit --skip-editable -f cyclonedx-json -o sbom.cdx.json
if ($LASTEXITCODE -ne 0) { throw "SBOM generation failed" }
& $python -m piplicenses --format=markdown --with-urls --output-file THIRD_PARTY_LICENSES.md
if ($LASTEXITCODE -ne 0) { throw "Third-party license report failed" }

Write-Host "Release quality gate passed."
