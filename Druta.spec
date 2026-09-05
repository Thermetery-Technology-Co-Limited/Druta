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
