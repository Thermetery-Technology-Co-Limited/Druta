<#
.SYNOPSIS
    Builds Druta as a onedir PyInstaller bundle and packages it into a
    local distributable zip with matching working-tree source.

.DESCRIPTION
    1. Runs PyInstaller against Druta.spec via `python -m PyInstaller`
       (bare `pyinstaller` is not on PATH on this box).
    2. Copies i2c/ to dist/Druta/i2c/, landing it directly beside Druta.exe.
       Druta.spec already bundles i2c/ under dist/Druta/_internal/i2c/ as a
       fallback (the loader checks both places), but the copy beside the
       exe is the one users are expected to hand-add their own regulator
       profiles into, so it must exist unconditionally - see the placement
       comment in Druta.spec.
    3. Copies the build's source into dist/Druta/source/ and writes a
       SHA-256 manifest. The source allowlist is checked before and after
       PyInstaller so edits during the build cannot silently mismatch it.
    4. Zips the resulting dist/Druta/ directory (including source and the
       beside-exe i2c/) into dist/Druta-<version>-win64.zip.

    Version comes from a VERSION / __version__-style constant grepped out of
    src/druta/druta.py at build time. This script does not own druta.py and will not
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
$drutaPy   = Join-Path $root 'src/druta/druta.py'
$sourceDir = Join-Path $bundleDir 'source'

# Keep this explicit: a working tree also contains private research, session
# profiles, and generated files that must not become part of a distribution.
# Copy current bytes, including uncommitted fixes, rather than a git archive
# of HEAD (which may describe a different executable).
function Get-SourceSnapshot {
    $paths = @(
        'druta.py', 'src/run_druta.py', 'src/druta/__init__.py', 'src/druta/__main__.py',
        'Druta.spec', 'build.ps1', 'requirements.txt',
        'pyproject.toml', 'setup.py', 'MANIFEST.in',
        '.github/PULL_REQUEST_TEMPLATE/i2c_profile.md',
        'AGENTS.md', 'COPYING', 'THIRD-PARTY-NOTICES.md', 'README.md', 'MANUAL.md',
        'TECHNICALDOCUMENTATION.md', 'DEBUG-SUMMARY-RTX5080.md',
        'VOLTAGE-RAILS-TITAN.md', 'VOLTAGE-RAILS-47212.md', 'DRIVER-COMPATIBILITY.md',
        'RELEASE-NOTES-1.3.0.md', 'MAXWELL-PASCAL-VALIDATION.md', 'BLACKWELL-VALIDATION.md',
        'experiments/voltage-rails-20260906.json',
        'experiments/legacy-offsets-47212-0000-01-00.0.json',
        'experiments/legacy-offsets-47212-0000-02-00.0.json',
        'experiments/legacy-frequency-production-47212.json',
        'experiments/compatibility-validation-47212.json',
        'experiments/kepler-validation-47212.json',
        'experiments/kepler-gtx690-validation-47212.json',
        'experiments/kepler-gtx690-timing-sweep-47212.json',
        'experiments/kepler-gtx690-i2c-47212.md',
        'experiments/mp2888a-discovery-47212.json',
        'experiments/legacy-private-layout-47212.json',
        'experiments/kepler-gtx690-clock-domains.json',
        'experiments/kepler-gtx690-clock-domains.md',
        'experiments/kepler-gtx770-clock-crosscheck.json',
        'experiments/kepler-gtx770-clock-crosscheck.md',
        'experiments/maxwell-gtx745-validation-47212.json',
        'experiments/maxwell-gtx745-clock-domains.json',
        'experiments/maxwell-gtx745-clock-domains.md',
        'experiments/maxwell-gtx745-p0-paths-47212.json',
        'experiments/maxwell-gtx745-p0-fan-ui-47212.json',
        'experiments/maxwell-gtx745-timing-sweep-47212.json',
        'experiments/maxwell-gtx745-timing-sweep-47212.md',
        'experiments/kepler-timing-writes-47212.json',
        'experiments/kepler-timing-sweep-47212.json',
        'experiments/kepler-ncp4206-identity-47212.json',
        'experiments/kepler-ncp4206-control-47212.json'
    )
    # Only the explicitly public measurement files above are included
    # from experiments/. Other research/session captures remain excluded.
    # Include the complete package and regression suite, including package
    # initializers and test helpers. Restrict recursion to these source trees;
    # private docs/, drv/, profiles/, and other experiments remain excluded.
    foreach ($directory in @('src/druta', 'tests')) {
        $paths += @(Get-ChildItem -LiteralPath (Join-Path $root $directory) -Filter '*.py' -File -Recurse |
            ForEach-Object { $_.FullName.Substring($root.Length + 1).Replace('\', '/') })
    }
    foreach ($pattern in @('i2c/*.toml', 'i2c/*.md')) {
        $paths += @(Get-ChildItem -Path (Join-Path $root $pattern) -File -ErrorAction SilentlyContinue |
            ForEach-Object { $_.FullName.Substring($root.Length + 1).Replace('\', '/') })
    }
    foreach ($relative in ($paths | Sort-Object -Unique)) {
        $path = Join-Path $root $relative
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Required source file is missing: $relative"
        }
        $item = Get-Item -LiteralPath $path
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Source file must not be a link: $relative"
        }
        [PSCustomObject]@{
            path = $relative
            bytes = $item.Length
            sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
}

$sourceBefore = @(Get-SourceSnapshot)

# --- Step 1: PyInstaller -----------------------------------------------
Write-Host "==> [1/5] python -m PyInstaller --noconfirm Druta.spec"

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
Write-Host "==> [2/5] copying i2c/ -> dist/Druta/i2c/"

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

# --- Step 3: matching working-tree source -------------------------------
Write-Host "==> [3/5] packaging matching source -> dist/Druta/source/"

$sourceAfter = @(Get-SourceSnapshot)
$sourceChange = Compare-Object $sourceBefore $sourceAfter -Property path, bytes, sha256
if ($sourceChange) {
    throw 'Source changed during the build. Run build.ps1 again after editing finishes; no new archive was made.'
}
if (Test-Path -LiteralPath $sourceDir) {
    throw "Expected a fresh PyInstaller output, but $sourceDir already exists. Refusing to mix source snapshots."
}
New-Item -ItemType Directory -Path $sourceDir | Out-Null
foreach ($entry in $sourceAfter) {
    $destination = Join-Path $sourceDir $entry.path
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $root $entry.path) -Destination $destination
    $copiedHash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($copiedHash -ne $entry.sha256) {
        throw "Source changed while copying $($entry.path). Run build.ps1 again; no new archive was made."
    }
}

$gitCommit = $null
$workingTreeChanges = $null
if ((Test-Path -LiteralPath (Join-Path $root '.git')) -and (Get-Command git -ErrorAction SilentlyContinue)) {
    $revision = git -C $root rev-parse HEAD 2>$null
    if ($LASTEXITCODE -eq 0) {
        $gitCommit = "$revision".Trim()
        $gitStatus = @(git -C $root status --porcelain --untracked-files=normal 2>$null)
        if ($LASTEXITCODE -eq 0) { $workingTreeChanges = ($gitStatus.Count -gt 0) }
    }
}
$manifest = [ordered]@{
    format = 1
    created_utc = [DateTime]::UtcNow.ToString('o')
    source_kind = 'working-tree snapshot, including uncommitted files'
    git_commit = $gitCommit
    git_working_tree_changes = $workingTreeChanges
    executable = 'Druta.exe'
    executable_sha256 = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash.ToLowerInvariant()
    files = $sourceAfter
}
$manifestPath = Join-Path $sourceDir 'SOURCE-MANIFEST.json'
[IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 5) + "`n", [Text.UTF8Encoding]::new($false))

# --- Step 4: version from druta.py --------------------------------------
Write-Host "==> [4/5] resolving version from druta.py"

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

# --- Step 5: zip ----------------------------------------------------------
Write-Host "==> [5/5] compressing dist/Druta/ -> $zipName"

Compress-Archive -Path $bundleDir -DestinationPath $zipPath -CompressionLevel Optimal -Force

$zipItem = Get-Item $zipPath
$zipSizeMB = [Math]::Round($zipItem.Length / 1MB, 2)

Write-Host ""
Write-Host "==> Build complete."
Write-Host "    EXE path : $exePath"
Write-Host "    Source   : $sourceDir"
Write-Host "    Manifest : $manifestPath"
Write-Host "    Zip path : $($zipItem.FullName)"
Write-Host "    Zip size : $($zipItem.Length) bytes ($zipSizeMB MB)"
