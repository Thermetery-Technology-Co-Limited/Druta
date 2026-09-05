# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

# GPL-3.0 section 4, reached for object code through section 6's chapeau
# ("under the terms of sections 4 and 5"), requires that you "give all
# recipients a copy of this License along with the Program". This is a ONEDIR
# build: PyInstaller places `datas` entries under _internal/, so these two
# land there - but in a onedir tree _internal/ sits directly beside the
# visible Druta.exe, not buried inside a self-extracting archive like it was
# in the old onefile build. A recipient can see COPYING and
# THIRD-PARTY-NOTICES.md by opening the shipped folder, no extraction step
# required, which is a stronger compliance posture than onefile, not a
# weaker one. Help > Licences still reads them back out of the bundle at
# runtime (see druta.py resource_path).
#
# The third entry, i2c/, is unrelated to licensing: it is the regulator
# profile directory (see i2c/PROFILES.md). Bundling it here puts a copy under
# _internal/i2c/ as a fallback; the copy that matters for users - a plain
# i2c/ folder directly beside Druta.exe, so hand-added profiles don't have to
# go under _internal/ - is placed by build.ps1 after PyInstaller runs, since
# the spec has no mechanism to place a data folder beside the exe in onedir.
datas = [('COPYING', '.'), ('THIRD-PARTY-NOTICES.md', '.'), ('i2c', 'i2c')]
binaries = []
hiddenimports = []
tmp_ret = collect_all('dearpygui')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


# KNOWN AND DELIBERATELY NOT "FIXED": Analysis collects the Visual C++ runtime
# from three different origins, so the shipped bundle carries three CRT
# versions at once.
#
#   MSVCP140.dll                14.51  <- C:\Windows\System32
#   VCRUNTIME140_1.dll          14.51  <- C:\Windows\System32
#   VCRUNTIME140.dll            14.42  <- the Python installation
#   dearpygui/vcruntime140_1.dll 14.28 <- bundled inside the dearpygui wheel
#
# Microsoft's guidance is to deploy the runtime as a matched set, and pairing a
# 14.51 MSVCP140 with a 14.42 VCRUNTIME140 is the unsupported direction (newer
# consumer, older provider). It therefore looks like an obvious suspect the
# first time a bundled build faults inside MSVCP140, and it was investigated as
# exactly that.
#
# IT IS NOT ONE, on the evidence:
#   - The crash that prompted the investigation was root-caused elsewhere
#     entirely, to a Dear PyGui call made before create_context(). That
#     reproduces 3/3 from a bare python.exe against the SYSTEM MSVCP140, with
#     no bundle, no _internal/ and no PyInstaller involved, at a byte-identical
#     fault offset. Packaging was not on the path.
#   - The version gap has no import-surface consequence here: MSVCP140 14.51
#     imports 17 symbols from VCRUNTIME140, all 17 are exported by the bundled
#     14.42, and 14.51 exports nothing that 14.42 lacks. A genuinely missing
#     entry point would fail the load outright rather than access-violate.
#
# So this stays as it is. Pinning the origins means overriding PyInstaller's
# CRT collection, which is easy to get subtly wrong, and it would be a change
# to a build that is measured working in exchange for no demonstrated defect.
# The note exists so the next person to see MSVCP140 in a crash dump does not
# spend the afternoon re-deriving that it was a red herring. If the CRT ever
# does become implicated, the evidence to gather first is whether the fault
# reproduces OUTSIDE the bundle - if it does, this is not the cause.
a = Analysis(
    ['druta.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name='Druta',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # False, not True. It was True, but upx is not installed here so PyInstaller
    # silently skipped it - the shipped binary has no UPX stub (verified). A
    # flag that does nothing on the build machine but would silently add a
    # component to the binary on a machine that happens to have upx installed
    # is worse than no flag, now that the bundle's contents are a licensing
    # question (see THIRD-PARTY-NOTICES.md).
    upx=False,
    upx_exclude=[],
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# onedir: COLLECT gathers the exe, the shared binaries/datas it was built
# with exclude_binaries=True to keep out of, and everything else into
# dist/Druta/ as a directory tree instead of a single self-extracting file.
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Druta',
)
