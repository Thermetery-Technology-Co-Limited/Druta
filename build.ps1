<#
.SYNOPSIS
    Builds Druta as a onedir PyInstaller bundle and packages it into a
    distributable zip.

.DESCRIPTION
    1. Runs PyInstaller against Druta.spec via `python -m PyInstaller`
       (bare `pyinstaller` is not on PATH on this box).
    2. Copies i2c/ to dist/Druta/i2c/, landing it directly beside Druta.exe.
       Druta.spec already bundles i2c/ under dist/Druta/_internal/i2c/ as a
       fallback (the loader checks both places), but the copy beside the
       exe is the one users are expected to hand-add their own regulator
       profiles into, so it must exist unconditionally - see the placement
       comment in Druta.spec.
    3. Zips the resulting dist/Druta/ directory (post-copy, so the beside-
       exe i2c/ is included) into dist/Druta-<version>-win64.zip.

    Version comes from a VERSION / __version__-style constant grepped out of
    druta.py at build time. This script does not own druta.py and will not
    add one: if no such constant is found, the zip is named with "dev"
    instead of an invented version number.

.NOTES
    Re-running this script is safe: PyInstaller --noconfirm refreshes
    dist/Druta/ in place, the i2c/ copy is Copy-Item -Force (overwrites,
    never deletes first), and the zip step overwrites only its own prior
    zip via Compress-Archive -Force. Nothing under dist/ or build/ is ever
    removed wholesale.
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root      = $PSScriptRoot
$distDir   = Join-Path $root 'dist'
$bundleDir = Join-Path $distDir 'Druta'
$exePath   = Join-Path $bundleDir 'Druta.exe'
$specPath  = Join-Path $root 'Druta.spec'
$i2cSrc    = Join-Path $root 'i2c'
$i2cDst    = Join-Path $bundleDir 'i2c'
$drutaPy   = Join-Path $root 'druta.py'

# --- Step 1: PyInstaller -----------------------------------------------
Write-Host "==> [1/4] python -m PyInstaller --noconfirm Druta.spec"

Push-Location $root
try {
    python -m PyInstaller --noconfirm $specPath
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller exited with code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

if (-not (Test-Path $exePath)) {
    throw "Expected build output not found at $exePath - aborting before packaging."
}

# --- Step 2: bundle i2c/ beside the exe ---------------------------------
Write-Host "==> [2/4] copying i2c/ -> dist/Druta/i2c/"

if (-not (Test-Path $i2cSrc)) {
    throw "Source i2c/ directory not found at $i2cSrc"
}
Copy-Item -Path $i2cSrc -Destination $i2cDst -Recurse -Force

# The licence texts too, for the same reason and one stronger. PyInstaller puts
# spec `datas` under _internal/, and a GPL-3.0 licence a recipient has to go
# spelunking for is a poor reading of section 4's "give all recipients a copy of
# this License along with the Program". Moving to onedir was supposed to make
# these MORE visible than they were inside a self-extracting exe, not less, so
# they get copied beside the exe as well. The _internal copies stay - Help >
# Licences reads those back at runtime via resource_path.
foreach ($lic in @('COPYING', 'THIRD-PARTY-NOTICES.md')) {
    $src = Join-Path $root $lic
    if (Test-Path $src) {
        Copy-Item -Path $src -Destination (Join-Path $bundleDir $lic) -Force
    }
    else {
        Write-Warning "$lic not found at repo root - the shipped bundle will not carry it beside the exe."
    }
}

# --- Step 3: version from druta.py --------------------------------------
Write-Host "==> [3/4] resolving version from druta.py"

$version = $null
if (Test-Path $drutaPy) {
    $pattern = '(?im)^\s*(?:VERSION|__version__|APP_VERSION|DRUTA_VERSION)\s*=\s*[''"]([^''"]+)[''"]'
    $found = Select-String -Path $drutaPy -Pattern $pattern | Select-Object -First 1
    if ($found) {
        $version = $found.Matches[0].Groups[1].Value
    }
}

if (-not $version) {
    Write-Warning "No VERSION/__version__-style constant found in druta.py. Using 'dev' rather than inventing a version number (druta.py is out of scope for this build script)."
    $version = 'dev'
}
else {
    Write-Host "    found: $version"
}

$zipName = "Druta-$version-win64.zip"
$zipPath = Join-Path $distDir $zipName

# --- Step 4: zip ----------------------------------------------------------
Write-Host "==> [4/4] compressing dist/Druta/ -> $zipName"

Compress-Archive -Path $bundleDir -DestinationPath $zipPath -CompressionLevel Optimal -Force

$zipItem = Get-Item $zipPath
$zipSizeMB = [Math]::Round($zipItem.Length / 1MB, 2)

Write-Host ""
Write-Host "==> Build complete."
Write-Host "    Zip path : $($zipItem.FullName)"
Write-Host "    Zip size : $($zipItem.Length) bytes ($zipSizeMB MB)"
