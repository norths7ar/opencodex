[CmdletBinding()]
param(
    [string]$BinDirectory = (Join-Path $env:USERPROFILE '.local\bin'),
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$entryPoint = Join-Path $projectRoot '.venv\Scripts\python.exe'
$binPath = [IO.Path]::GetFullPath($BinDirectory)
$marker = 'OpenCodex Lite source checkout launcher'
$targets = foreach ($name in @('ocx', 'opencodex')) {
    foreach ($extension in @('ps1', 'cmd')) {
        Join-Path $binPath "$name.$extension"
    }
}

# Check every destination before replacing any launcher.
foreach ($target in $targets) {
    if ((Test-Path -LiteralPath $target) -and
        !(Get-Content -LiteralPath $target -Raw).Contains($marker)) {
        throw "Another command occupies $target. Resolve that conflict before registering Lite."
    }
}

if ($Remove) {
    foreach ($target in $targets) {
        if (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target
        }
    }
    Write-Output "Removed Lite command launchers from $binPath."
    return
}

if (!(Test-Path -LiteralPath $entryPoint -PathType Leaf)) {
    throw 'The project environment is missing. Run uv sync in the project first.'
}
foreach ($name in @('ocx', 'opencodex')) {
    foreach ($command in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
        if ($command.Source -notin $targets) {
            throw "Existing command $name resolves to $($command.Definition). Resolve that conflict first."
        }
    }
}

$psEntry = $entryPoint.Replace("'", "''")
$cmdEntry = $entryPoint.Replace('%', '%%')
$powershellLauncher = @"
# $marker
& '$psEntry' -I -m backend.runtime @args
exit `$LASTEXITCODE
"@
$cmdLauncher = @"
@echo off
rem $marker
"$cmdEntry" -I -m backend.runtime %*
exit /b %errorlevel%
"@
New-Item -ItemType Directory -Path $binPath -Force | Out-Null
foreach ($target in $targets) {
    $contents = if ($target.EndsWith('.ps1')) { $powershellLauncher } else { $cmdLauncher }
    [IO.File]::WriteAllText($target, $contents + "`r`n", [Text.UTF8Encoding]::new($false))
}

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$entries = @($userPath -split ';' | Where-Object { $_ })
$expandedEntries = @($entries | ForEach-Object {
    [Environment]::ExpandEnvironmentVariables($_).TrimEnd('\', '/')
})
if ($binPath.TrimEnd('\', '/') -notin $expandedEntries) {
    [Environment]::SetEnvironmentVariable('Path', (($entries + $binPath) -join ';'), 'User')
}
if ($binPath.TrimEnd('\', '/') -notin @($env:PATH -split ';' | ForEach-Object { $_.TrimEnd('\', '/') })) {
    $env:PATH += ";$binPath"
}
Write-Output "Registered ocx and opencodex in $binPath."
Write-Output "Source: $projectRoot"
