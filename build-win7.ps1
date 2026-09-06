<#
.SYNOPSIS
Builds the separate Windows 7 SP1 x64 distribution on a Windows build host.
.DESCRIPTION
Requires CPython 3.8.10 x64, requirements-win7.txt, and the matched x64
Visual C++ 2019 14.29.30157 runtime files. See WINDOWS7.md.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$CrtDirectory
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$root = $PSScriptRoot
$Python = (Get-Command $Python -ErrorAction Stop).Source
$crtPath = (Resolve-Path -LiteralPath $CrtDirectory).Path
$previousCrt = [Environment]::GetEnvironmentVariable('DRUTA_WIN7_CRT', 'Process')
Push-Location $root
try {
    $env:DRUTA_WIN7_CRT = $crtPath
    & $Python -m PyInstaller --noconfirm --clean --workpath build\pyinstaller-win7 Druta-win7.spec
    if ($LASTEXITCODE -ne 0) { throw "Windows 7 build failed ($LASTEXITCODE)." }
    $bundle = Join-Path $root 'dist\Druta-Win7'
    if (-not (Test-Path -LiteralPath (Join-Path $bundle 'Druta.exe'))) {
        throw 'Missing Druta-Win7\Druta.exe.'
    }
    # Copy contents, not the directory itself: reruns must not nest i2c/i2c.
    $i2c = Join-Path $bundle 'i2c'
    New-Item -ItemType Directory -Path $i2c -Force | Out-Null
    Copy-Item -Path (Join-Path $root 'i2c\*') -Destination $i2c -Recurse -Force
    foreach ($notice in @('COPYING', 'THIRD-PARTY-NOTICES.md', 'WINDOWS7.md')) {
        Copy-Item -LiteralPath (Join-Path $root $notice) -Destination $bundle -Force
    }
    $licenses = Join-Path $bundle 'licenses'
    New-Item -ItemType Directory -Path $licenses -Force | Out-Null
    Copy-Item -Path (Join-Path $bundle '_internal\licenses\*') -Destination $licenses -Recurse -Force
    & $Python -m pip freeze | Set-Content -LiteralPath (Join-Path $bundle 'BUILD-DEPENDENCIES.txt') -Encoding UTF8
    if ($LASTEXITCODE -ne 0) { throw 'Could not record build dependencies.' }
    $zip = Join-Path $root 'dist\Druta-dev-win7-x64.zip'
    Compress-Archive -LiteralPath $bundle -DestinationPath $zip -Force
    Write-Host "Windows 7 bundle: $zip"
}
finally {
    [Environment]::SetEnvironmentVariable('DRUTA_WIN7_CRT', $previousCrt, 'Process')
    Pop-Location
}
