$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$exe = Join-Path $projectRoot "dist\THMedia.exe"
if (-not (Test-Path -LiteralPath $exe)) { throw "Missing dist\THMedia.exe" }

$smokeRoot = Join-Path $env:TEMP ("flow-story-smoke-" + [guid]::NewGuid().ToString("N"))
$workspace = Join-Path $smokeRoot "workspace"
$readyFile = Join-Path $smokeRoot "ready.txt"
New-Item -ItemType Directory -Path $workspace -Force | Out-Null
$process = $null
try {
  $process = Start-Process -FilePath $exe -ArgumentList @(
    "--smoke-backend", "--workspace", $workspace, "--ready-file", $readyFile
  ) -PassThru
  $deadline = (Get-Date).AddSeconds(60)
  while (-not (Test-Path -LiteralPath $readyFile)) {
    $process.Refresh()
    if ($process.HasExited) { throw "Smoke EXE exited before backend became ready (exit $($process.ExitCode))" }
    if ((Get-Date) -gt $deadline) { throw "Smoke EXE did not publish its backend address within 60 seconds" }
    Start-Sleep -Milliseconds 250
  }

  $baseUrl = (Get-Content -LiteralPath $readyFile -Raw).Trim()
  $health = $null
  while (-not $health -and (Get-Date) -le $deadline) {
    $process.Refresh()
    if ($process.HasExited) { throw "Smoke EXE exited before health check succeeded (exit $($process.ExitCode))" }
    try {
      $health = Invoke-RestMethod -Uri "$baseUrl/api/health" -TimeoutSec 2
    }
    catch {
      Start-Sleep -Milliseconds 300
    }
  }
  if (-not $health) { throw "Smoke EXE backend did not answer /api/health within 60 seconds" }
  if (-not $health.ok) { throw "Smoke health endpoint returned ok=false" }
  if (-not $health.version) { throw "Smoke health endpoint did not report a version" }
  Write-Host "EXE smoke passed: $baseUrl version $($health.version)"
}
finally {
  if ($process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force }
  Remove-Item -LiteralPath $smokeRoot -Recurse -Force -ErrorAction SilentlyContinue
}
