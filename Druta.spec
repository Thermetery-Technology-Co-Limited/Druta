# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = []
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
    a.binaries,
    a.datas,
    [],
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
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
