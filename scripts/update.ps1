[CmdletBinding()]
param([string]$StateDir)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if ($StateDir) {
    $StateDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($StateDir)
}
$scratch = Join-Path $projectRoot '.tmp'
New-Item -ItemType Directory -Force -Path $scratch | Out-Null

# Serialize the shared build directory until the deployment has finished.
$lockPath = Join-Path $scratch 'frontend-build-update.lock'
try {
    $buildLock = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None')
} catch [IO.IOException] {
    throw 'Cannot lock the frontend update directory. Check for another update in progress.'
}

Push-Location -LiteralPath $projectRoot
try {
    $uv = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue
    $uvPath = if ($uv) { $uv.Source } else { Join-Path $env:USERPROFILE '.local\bin\uv.exe' }
    & $uvPath sync --locked
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed ($LASTEXITCODE); the console was not published." }

    & pnpm --dir frontend install --frozen-lockfile
    if ($LASTEXITCODE -ne 0) { throw "pnpm install failed ($LASTEXITCODE); the console was not published." }

    & pnpm --dir frontend build:runtime
    if ($LASTEXITCODE -ne 0) { throw "Frontend build failed ($LASTEXITCODE); the console was not published." }

    $python = Join-Path $projectRoot '.venv\Scripts\python.exe'
    $deploymentArgs = @('-m', 'backend.runtime.deployment')
    if ($StateDir) { $deploymentArgs += @('--state-dir', $StateDir) }
    & $python @deploymentArgs
    exit $LASTEXITCODE
} finally {
    Pop-Location
    $buildLock.Dispose()
}
