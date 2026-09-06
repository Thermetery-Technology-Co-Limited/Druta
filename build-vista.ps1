<#
.SYNOPSIS
Builds the portable Vista SP2 x64 distribution from its patched runtime.
.DESCRIPTION
Build the three small native source changes described in VISTA.md first.
The matched VC2019 and exact SDK 10240 app-local UCRT set collected by
tools/collect_vista_redist.py are mandatory for this build.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$CrtDirectory,
    [Parameter(Mandatory=$true)][string]$PortableRuntimeDirectory,
    [string]$NvtunePackageDirectory = ''
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Python = (Get-Command $Python -ErrorAction Stop).Source
$crtPath = (Resolve-Path -LiteralPath $CrtDirectory).Path
$portablePath = (Resolve-Path -LiteralPath $PortableRuntimeDirectory).Path
$nvtunePath = if ($NvtunePackageDirectory) { (Resolve-Path -LiteralPath $NvtunePackageDirectory).Path } else { '' }
$previousCrt = [Environment]::GetEnvironmentVariable('DRUTA_WIN7_CRT', 'Process')
$previousPortable = [Environment]::GetEnvironmentVariable('DRUTA_WIN7_PORTABLE_REDIST', 'Process')
Push-Location $PSScriptRoot
try {
    $env:DRUTA_WIN7_CRT = $crtPath
    $env:DRUTA_WIN7_PORTABLE_REDIST = $portablePath
    & $Python -m PyInstaller --noconfirm --clean --workpath build\pyinstaller-vista Druta-vista.spec
    if ($LASTEXITCODE -ne 0) { throw "Vista build failed ($LASTEXITCODE)." }
    $bundle = Join-Path $PSScriptRoot 'dist\Druta-Vista-Portable'
    if (-not (Test-Path -LiteralPath (Join-Path $bundle 'Druta.exe'))) { throw 'Missing portable Druta.exe.' }
    & $Python -m pip freeze | Set-Content -LiteralPath (Join-Path $bundle 'BUILD-DEPENDENCIES.txt') -Encoding UTF8
    if ($LASTEXITCODE -ne 0) { throw 'Could not record build dependencies.' }
    if ($nvtunePath) {
        & $Python tools\package_vista_nvtune.py --source $nvtunePath --bundle $bundle
        if ($LASTEXITCODE -ne 0) { throw 'The optional nvtune package failed validation.' }
    }
    $archive = Join-Path $PSScriptRoot 'dist\Druta-dev-vista-x64-portable.zip'
    Compress-Archive -LiteralPath $bundle -DestinationPath $archive -Force
    Write-Host "Vista bundle: $archive"
}
finally {
    [Environment]::SetEnvironmentVariable('DRUTA_WIN7_CRT', $previousCrt, 'Process')
    [Environment]::SetEnvironmentVariable('DRUTA_WIN7_PORTABLE_REDIST', $previousPortable, 'Process')
    Pop-Location
}
