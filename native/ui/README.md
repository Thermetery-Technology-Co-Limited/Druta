# Native frontend preview

This is the first C++ visual shell from [the UI/UX plan](../DESIGN-NATIVE-UI.md).
Focused and Classic / Verbose share local draft state, with Daily, Advanced and
Monitor views. All readings are labelled fixtures. The preview does not load
`drutacore`, open a GPU tuning handle, read real telemetry or write GPU settings.
Its sample counter exercises the future telemetry cadence only.

## Build on Windows

Run from the repository root with CMake 3.20+, Visual Studio 2022 C++ tools and
a Windows SDK available. Python 3 is needed by the build's PE import audit;
pytest is needed for the Python test suite. The resulting EXE needs no Python
installation. Dear ImGui and ImPlot are pinned in [vendor/](vendor/README.md),
with their license texts and byte checksums; configuration/build needs no downloads.

Standalone UI build, without building the tuning core:

```powershell
cmake -S native/ui -B native/build/ui-msvc-x64 -G "Visual Studio 17 2022" -A x64
cmake --build native/build/ui-msvc-x64 --config Release
ctest --test-dir native/build/ui-msvc-x64 -C Release --output-on-failure
```

If CMake finds the wrong Python, configure with
`-DPython3_EXECUTABLE=C:/path/to/python.exe`. The audit runs in the default build
and writes `native/build/ui-msvc-x64/Release/imports.json`. To run it explicitly:

```powershell
python native/ui/tools/audit_imports.py native/build/ui-msvc-x64/Release/druta-ui.exe --report native/build/ui-msvc-x64/Release/imports.json
python -m pytest -q native/ui/tests/test_audit_imports.py
```

The parent `native/CMakeLists.txt` also supports `-DDRUTA_BUILD_UI=ON`; it defaults
to `OFF`, leaving ordinary core builds unchanged. UI options/import policy stay
separate from the core's XP policy.

## Run and capture

```powershell
& ./native/build/ui-msvc-x64/Release/druta-ui.exe
& ./native/build/ui-msvc-x64/Release/druta-ui.exe --classic --page monitor --width 1366 --height 768
& ./native/build/ui-msvc-x64/Release/druta-ui.exe --scale 1.5 --capture C:/existing-folder/focused.bmp
```

| Option | Behavior |
| --- | --- |
| `--width N`, `--height N` | Initial client size in pixels; each accepts 640–7680. |
| `--scale N` | Explicit UI scale from 1 through 3, including fractional values. |
| `--classic` | Start in Classic / Verbose instead of Focused. |
| `--page daily\|advanced\|monitor` | Choose the initial page. |
| `--capture FILE.bmp` | Save a rendered BMP and exit; the parent directory must exist. |
| `--smoke-test EXISTING_DIRECTORY` | Exercise the real window/renderer loop, save BMPs and `smoke.json`, then exit. |

For a smoke run that waits for completion and exposes the exit code:

```powershell
New-Item -ItemType Directory -Force native/build/ui-smoke | Out-Null
$outputDir = (Resolve-Path native/build/ui-smoke).Path
$preview = Start-Process ./native/build/ui-msvc-x64/Release/druta-ui.exe -ArgumentList @('--smoke-test', ('"' + $outputDir + '"')) -Wait -PassThru
$preview.ExitCode
```

The smoke sequence covers both presentations, page changes, retained drafts,
ordinary/continuous minimized behavior, explicit Pause, restoration and resize.
Counters are render calls, presents and fixture ticks, not GPU utilization or
real sensor reads. Hotkey delivery is injected; it does not prove physical keys.

## Refresh and renderer behavior

**Always refresh** starts off. Interaction targets 60 FPS; after 0.75 seconds
without activity, visible rendering targets 10 FPS. Minimize with the toggle off
to stop rendering and fixture sample ticks. Turn it on to keep both running when
minimized. **Pause** overrides the toggle and minimizes the window.

Restore from the taskbar or use **Ctrl+Shift+F11**. If another process owns that
shortcut, taskbar restoration remains available. The scheduler keeps required
maintenance deadlines independent of frame pacing; this fixture preview owns
no active hardware mode that requires such maintenance.

Interactive waits use an optional high-resolution one-shot timer when the OS
accepts it, falling back to an ordinary waitable timer. Idle windows use coarse
waits; paused windows wait for messages. No `timeBeginPeriod` request is made.

Rendering uses Direct3D 9. Its software vertex-processing fallback is still a
D3D9 renderer, not a CPU rasterizer. Resize/device-reset handling lives in the
main loop. The current Win32 move/size modal loop pauses frame submission during
a border drag; smooth rendering inside that modal loop remains follow-up work.

## Qualification checkpoint

- **2026-11-11, Windows 10 x64 build 19045, RTX 3070 Ti:** standalone and parent
  opt-in Release builds passed with MSVC 19.44.35230 / SDK 10.0.26100.0. Both CTest
  entries passed: 48 scheduler checks and 16 PE-policy tests. All 20 vendored
  source files matched their recorded SHA256 hashes.
- **Rendered review:** reviewed Focused, Classic, Advanced and Monitor;
  1024×768 and 1366×768, with 100/125/150/200% scale captures. At short/high-DPI
  sizes the workspace scrolls. This is visual review, not full accessibility
  qualification or a physical multi-monitor DPI transition test.
- **Window/device lifecycle:** all 11 two-second smoke phases passed, including
  four successful D3D9 resets, retained draft through presentations/pages,
  minimize, Pause and injected hotkey restoration. Ordinary minimized and
  explicit Pause phases produced zero frames and zero fixture sample ticks;
  Always refresh produced 115 frames/presents over two seconds while minimized.
  This is not a GPU-utilization benchmark or real hardware telemetry test.
- **Keyboard/mouse:** the Computer Use capture helper timed out twice, so
  physical entry/click verification remains pending. Keyboard navigation is
  enabled; smoke hotkey delivery is injected, not a physical shortcut test.
- **Vista/7:** first-version targets, not qualified by this modern MSVC build.
  A suitable legacy toolchain and actual launch/render/font/DPI/device-loss tests
  on Vista SP2 and Windows 7 SP1 remain required.
- **Import audit:** checks PE headers, expected DLLs and selected newer static
  APIs. Passing does not certify every imported function, static CRT behavior or
  actual legacy-OS execution. The broader platform audit remains open.
- **XP/x86 UI:** not qualified or promised by selecting D3D9. Core support is
  independent and must not be narrowed by this preview.

Keep the existing Python application and its experiment facade available while
native services and control parity are added. The next increment is the typed,
read-only session boundary; hardware tuning follows verified commands, readback,
scoped Undo and owned-control lifecycle work.

Raw smoke results and the tested EXE hash are recorded in
[validation/2026-11-11-smoke.json](validation/2026-11-11-smoke.json).
