# Vista dependency source patches

`dearpygui-vista.patch` applies to the unmodified Dear PyGui **v2.3.1** tag,
commit `719d068967ace64eb9ff4171f2052a0799fd6c35`, with its pinned submodules.
It preserves the existing interface and changes only Windows file-dialog
filesystem calls, their UTF-8 conversions, and an explicit software renderer
option. The application still uses the normal Dear PyGui 2.3.1 Python package.

## Dear PyGui changes

- Windows directory enumeration uses `FindFirstFileW` and `FindNextFileW`.
  The precompiled modern MSVC filesystem implementation uses `FindExInfoBasic`,
  which is unavailable on Vista. Filesystem status and directory creation use
  `GetFileAttributesW` and `CreateDirectoryW`, avoiding modern STL's `CreateFile2`.
  The existing file-dialog appearance, filtering and sorting remain in use.
- File-dialog strings convert explicitly between UTF-8 and UTF-16. The upstream
  locale-dependent conversions fail for paths outside the active code page.
  File size and date lookup uses `_wstat64` so those columns work in Unicode
  directories too.
- Only `DRUTA_D3D11_WARP=1` requests a WARP device. Unset, `0`, and other values
  preserve normal hardware adapter selection. This option changes the GUI
  renderer; it does not emulate NVIDIA hardware or tuning capabilities. Vista
  still needs its Direct3D 11 platform update.

The unused `dirent` fallback in the upstream file-dialog source references a
header absent from this tag. The patch therefore keeps its existing filesystem
configuration and replaces the incompatible Windows calls directly.

## Rebuild the extension

The validated build host used MSVC **14.51.36231** (Visual Studio 18 Community),
its bundled CMake and Ninja, and CPython **3.8.10 x64** development headers and
import library. Run the following from an x64 MSVC developer command prompt,
with `cmake` and `ninja` on `PATH`, in the Druta repository. Adjust only the
Python directory to match your isolated build runtime:

```bat
git clone --depth 1 --branch v2.3.1 --recurse-submodules --shallow-submodules https://github.com/hoffstadt/DearPyGui.git build\vista\dearpygui
git -C build\vista\dearpygui apply ..\..\..\tools\vista\dearpygui-vista.patch
set "DRUTA_BUILD_PYTHON=C:/Python38"
cmake -S build/vista/dearpygui -B build/vista/dearpygui/cmake-build-vista -G Ninja -DCMAKE_BUILD_TYPE=Release -DMVDIST_ONLY=True -DMVDPG_VERSION=2.3.1 -DMV_PY_VERSION=3.8 "-DPython_ROOT_DIR=%DRUTA_BUILD_PYTHON%" "-DPython_INCLUDE_DIR=%DRUTA_BUILD_PYTHON%/include" "-DPython_LIBRARY=%DRUTA_BUILD_PYTHON%/libs/python38.lib" "-DCMAKE_CXX_FLAGS=/DWIN32 /D_WINDOWS /W3 /GR /EHsc /D_WIN32_WINNT=0x0600 /DWINVER=0x0600 /D_DISABLE_CONSTEXPR_MUTEX_CONSTRUCTOR" "-DCMAKE_C_FLAGS=/DWIN32 /D_WINDOWS /W3 /D_WIN32_WINNT=0x0600 /DWINVER=0x0600" "-DCMAKE_SHARED_LINKER_FLAGS=/SUBSYSTEM:WINDOWS,6.00" -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DFT_WITH_ZLIB=OFF -DFT_WITH_BZIP2=OFF -DFT_WITH_PNG=OFF -DFT_WITH_HARFBUZZ=OFF -DFT_WITH_BROTLI=OFF
cmake --build build/vista/dearpygui/cmake-build-vista --config Release --parallel 8
```

The resulting extension is
`build/vista/dearpygui/cmake-build-local/DearPyGui/_dearpygui.pyd`.
Upstream's output directory name is independent of the CMake build directory.
Do not run upstream `setup.py` for this workflow: it selects its own generator
and rebuilds with different options.

The explicit `/EHsc`, `/GR`, `/DWIN32`, and `/D_WINDOWS` preserve CMake's normal
MSVC defaults when supplying `CMAKE_CXX_FLAGS`. The mutex compatibility define
preserves initialization compatible with the bundled VC++ 14.29 runtime.

All imported symbols from the resulting extension were checked against the
portable bundle's actual Microsoft VC++ **14.29.30157.0**, UCRT
**10.0.19041.5609**, and shader-compiler DLL exports. Its additional Windows
imports compared with the original wheel are only `GetEnvironmentVariableW`
and `GetFileAttributesW`, both available on Vista. Merely setting a PE minimum
does not establish runtime compatibility: run the target-OS checks too.

## Real file-dialog fixture

Use the assembled Vista Python runtime to test its installed extension:

```bat
python tools\smoke_vista_dialog.py --output dialog-hardware.json
python tools\smoke_vista_dialog.py --output dialog-warp.json --warp
```

To test the freshly built extension before assembling the runtime, pass
`--extension build\vista\dearpygui\cmake-build-local\DearPyGui\_dearpygui.pyd`
and `--crt-directory PATH_TO_MATCHED_VC2019`. The optional `--offscreen` keeps
the test window outside the desktop; its framebuffer PNG still captures the
actual rendered dialog. `--hold-seconds 10` leaves time for external inspection.

The fixture creates a temporary Unicode directory with a Unicode filename,
Unicode subdirectory and ASCII comparison file. It renders the real dialog,
checks its current path and default filename, records the frame count and
whether `d3d10warp.dll` loaded, and saves a PNG beside its JSON. Inspect that PNG
for the directory entries. It does not click or claim a completed user file
selection, and it never opens a GPU tuning backend. On the build host, hardware
and WARP runs each passed with 11 frames; their images visibly showed all three
fixture entries. Vista and Windows 7 results must be recorded separately.
