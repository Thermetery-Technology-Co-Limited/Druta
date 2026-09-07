# -*- mode: python ; coding: utf-8 -*-
"""Build with build-win7.ps1; do not collect a modern host's C/C++ runtime."""
import os
import sys
import hashlib
import json
from pathlib import Path
from importlib.metadata import distribution, version
from PyInstaller.utils.hooks import collect_all
import pefile

vista = globals().get('VISTA_BUILD', False)
if vista:
    sys.path.insert(0, str(Path('tools').resolve()))
    from vista_runtime import validate_runtime
    validate_runtime(sys.base_prefix)

if sys.version_info[:3] != (3, 8, 10) or sys.maxsize <= 2**32:
    raise SystemExit('Windows 7 builds require CPython 3.8.10 x64.')
for package, expected in [('dearpygui', '2.3.1'), ('pyinstaller', '6.16.0'),
                          ('tomli', '2.0.1')]:
    if version(package) != expected:
        raise SystemExit('Install requirements-win7.txt before building.')

crt = Path(os.environ['DRUTA_WIN7_CRT']).resolve(strict=True)
portable_redist = os.environ.get('DRUTA_WIN7_PORTABLE_REDIST')
if vista and not portable_redist:
    raise SystemExit('Vista builds always require the app-local portable runtimes.')
portable_files = []
portable_licenses = []
if portable_redist:
    portable_redist = Path(portable_redist).resolve(strict=True)
    if vista:
        from collect_vista_redist import validate as validate_vista_redist
        validate_vista_redist(portable_redist)
    manifest = json.loads((portable_redist / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format') != 1:
        raise SystemExit('Unsupported portable runtime manifest format.')
    for entry in manifest['files'] + manifest['licenses']:
        source = (portable_redist / entry['name']).resolve(strict=True)
        if portable_redist not in source.parents:
            raise SystemExit('Portable runtime path escapes its directory.')
        if hashlib.sha256(source.read_bytes()).hexdigest() != entry['sha256']:
            raise SystemExit('Portable runtime hash mismatch: ' + entry['name'])
    portable_files = [(entry['name'], str(portable_redist / entry['name']), 'BINARY')
                      for entry in manifest['files']]
    names = {entry[0].lower() for entry in portable_files}
    expected_dll_count = 42 if vista else 43
    if (len(portable_files) != expected_dll_count or len(names) != expected_dll_count or
            not {'ucrtbase.dll', 'd3dcompiler_47.dll'}.issubset(names) or
            any('/' in name or '\\' in name or not name.endswith('.dll') for name in names)):
        raise SystemExit('Expected the complete validated x64 UCRT and shader compiler set.')
    portable_licenses = [(str(portable_redist / entry['name']),
                          str(Path(entry['name']).parent))
                         for entry in manifest['licenses']]
crt_names = ('msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll')
for name in crt_names:
    dll = pefile.PE(str(crt / name))
    info = dll.VS_FIXEDFILEINFO[0]
    # A matched VS 2019 runtime; newer toolsets dropped Windows 7 support.
    actual = (info.FileVersionMS >> 16, info.FileVersionMS & 0xffff,
              info.FileVersionLS >> 16, info.FileVersionLS & 0xffff)
    if dll.FILE_HEADER.Machine != 0x8664 or actual != (14, 29, 30157, 0):
        raise SystemExit('CRT must be the x64 14.29.30157.0 set: ' + name)

datas, binaries, hiddenimports = collect_all('dearpygui')
datas += [('COPYING', '.'), ('THIRD-PARTY-NOTICES.md', '.'),
          ('WINDOWS7.md', '.'), ('i2c', 'i2c'),
          (str(Path(sys.base_prefix) / 'LICENSE.txt'), 'licenses/CPython-3.8.10')]
if portable_redist:
    datas += [('PORTABLE-WINDOWS7.md', '.')] + portable_licenses + [
        (str(portable_redist / 'manifest.json'), 'licenses/Microsoft-Windows-SDK')]
if vista:
    datas += [('VISTA.md', '.'), ('tools/vista', 'licenses/Vista-runtime-changes'),
              (str(Path(sys.base_prefix) / 'druta-vista-runtime.json'), 'licenses/Vista-runtime-changes')]
if (crt / 'VC2019-LICENSE.rtf').is_file():
    datas.append((str(crt / 'VC2019-LICENSE.rtf'), 'licenses/Microsoft-VC2019'))
# Ship the actual licenses from these distributions, including Tomli's MIT
# license and PyInstaller's bootloader exception, alongside the source notice.
for package in ('dearpygui', 'tomli', 'pyinstaller'):
    dist = distribution(package)
    for file in dist.files or []:
        if Path(str(file)).name.upper().startswith(('LICENSE', 'COPYING')):
            datas.append((str(dist.locate_file(file)), 'licenses/' + package))

a = Analysis(['druta.py'], pathex=[], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports, hookspath=[], hooksconfig={},
             # Druta has no TLS/network feature. CPython 3.8's OpenSSL 1.1.1
             # uses different terms than the modern build's OpenSSL 3; omit
             # these unused modules and keep hashlib's built-in fallbacks.
             runtime_hooks=[], excludes=['ssl', '_ssl', '_hashlib'],
             noarchive=False, optimize=0)

def is_system_runtime(name):
    name = Path(name).name.lower()
    return (name.startswith(('vcruntime', 'msvcp', 'concrt', 'api-ms-win-',
                             'ext-ms-win-')) or
            name in ('ucrtbase.dll', 'd3dcompiler_47.dll'))

# Universal CRT and D3DCompiler_47 are prerequisites for the compact build.
# Remove every collected copy, including the older CRT in Dear PyGui's wheel,
# then add a single matched set to _internal. Do not ship host system DLLs.
a.binaries = [entry for entry in a.binaries if not is_system_runtime(entry[0])]
a.binaries += [(name, str(crt / name), 'BINARY') for name in crt_names]
a.binaries += portable_files
if any(Path(entry[0]).name.lower().startswith(('libssl', 'libcrypto'))
       for entry in a.binaries):
    raise SystemExit('Unexpected OpenSSL dependency in Windows 7 build.')
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, exclude_binaries=True, name='Druta', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False,
          # Before Windows 8, app-local UCRT forwarders need the runtime beside
          # the main executable. Keep the portable distribution flat.
          contents_directory='.' if portable_redist else '_internal')
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False,
               name='Druta-Vista-Portable' if vista else ('Druta-Win7-Portable' if portable_redist else 'Druta-Win7'))
