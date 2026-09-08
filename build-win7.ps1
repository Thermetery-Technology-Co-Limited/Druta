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
    [Parameter(Mandatory=$true)][string]$CrtDirectory,
    [string]$PortableRuntimeDirectory = ''
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$root = $PSScriptRoot
$Python = (Get-Command $Python -ErrorAction Stop).Source
$crtPath = (Resolve-Path -LiteralPath $CrtDirectory).Path
$previousCrt = [Environment]::GetEnvironmentVariable('DRUTA_WIN7_CRT', 'Process')
$previousPortable = [Environment]::GetEnvironmentVariable('DRUTA_WIN7_PORTABLE_REDIST', 'Process')
$portablePath = if ($PortableRuntimeDirectory) { (Resolve-Path -LiteralPath $PortableRuntimeDirectory).Path } else { $null }
$bundleName = if ($portablePath) { 'Druta-Win7-Portable' } else { 'Druta-Win7' }
$workName = if ($portablePath) { 'pyinstaller-win7-portable' } else { 'pyinstaller-win7' }
$archiveName = if ($portablePath) { 'Druta-dev-win7-x64-portable.zip' } else { 'Druta-dev-win7-x64.zip' }
Push-Location $root
try {
    $env:DRUTA_WIN7_CRT = $crtPath
    [Environment]::SetEnvironmentVariable('DRUTA_WIN7_PORTABLE_REDIST', $portablePath, 'Process')
    $sourceSnapshot = Join-Path $root ('build\' + $workName + '-source.json')
    & $Python tools/package_source.py snapshot --root $root --snapshot $sourceSnapshot
    if ($LASTEXITCODE -ne 0) { throw 'Could not snapshot matching Windows 7 source.' }
    & $Python -m PyInstaller --noconfirm --clean --workpath (Join-Path 'build' $workName) Druta-win7.spec
    if ($LASTEXITCODE -ne 0) { throw "Windows 7 build failed ($LASTEXITCODE)." }
    $bundle = Join-Path (Join-Path $root 'dist') $bundleName
    if (-not (Test-Path -LiteralPath (Join-Path $bundle 'Druta.exe'))) {
        throw "Missing $bundleName\Druta.exe."
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
    if (-not $portablePath) {
        Copy-Item -Path (Join-Path $bundle '_internal\licenses\*') -Destination $licenses -Recurse -Force
    }
    & $Python -m pip freeze | Set-Content -LiteralPath (Join-Path $bundle 'BUILD-DEPENDENCIES.txt') -Encoding UTF8
    if ($LASTEXITCODE -ne 0) { throw 'Could not record build dependencies.' }
    & $Python tools/package_source.py package --root $root --snapshot $sourceSnapshot --bundle $bundle
    if ($LASTEXITCODE -ne 0) { throw 'Source changed or packaging failed; no new archive was made.' }
    $zip = Join-Path (Join-Path $root 'dist') $archiveName
    Compress-Archive -LiteralPath $bundle -DestinationPath $zip -Force
    Write-Host "Windows 7 bundle: $zip"
}
finally {
    [Environment]::SetEnvironmentVariable('DRUTA_WIN7_CRT', $previousCrt, 'Process')
    [Environment]::SetEnvironmentVariable('DRUTA_WIN7_PORTABLE_REDIST', $previousPortable, 'Process')
    Pop-Location
}
