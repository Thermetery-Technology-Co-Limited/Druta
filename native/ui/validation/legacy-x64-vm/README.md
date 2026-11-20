# Remote VM handoff for the Druta legacy x64 package

Run the native GUI and its dependencies inside **Windows 7 SP1 x64, Windows Vista SP2 x64, and Windows XP Professional x64 Edition SP2**. Return one evidence folder per guest. All three guest runs are pending. Use the same application ZIP for all three.

The application under test is merge commit `b7febbc2bf28408f87ef4adc89465f3bead839e4` on `cpp/core`. All 528 named imports across the four shipped binaries passed the three legacy export checks. Windows 10 x64 passed all 23 GUI checks in both fixture and live mode. The outstanding question is whether each legacy Windows loader and Direct3D 9 display driver can start and render this build. [Merged source](https://github.com/Thermetery-Technology-Co-Limited/Druta/commit/b7febbc2bf28408f87ef4adc89465f3bead839e4)

## Get the tested files onto the server

Copy the following ZIPs from the build workstation’s `Downloads` folder. Check both SHA-256 digests on the VM host before extraction. On Windows use `Get-FileHash -Algorithm SHA256 *.zip` in the transfer directory. On Linux use `sha256sum *.zip`.

| Archive | SHA-256 of the tested copy |
|---|---|
| `Druta-native-legacy-x64-b7febbc.zip` | `b7dcb291e189340c08fc172cf52beaada7fade40bc601eb8c21589297c91c409` |
| `Druta-legacy-x64-b7febbc-guest-check.zip` | `64b22bd738f11e40811ec12151a824853730ede459d84d7c8c763c7cc5bafb6d` |

Extract the application to `C:\Druta` and the kit to `C:\Druta-check` in **each guest**, with `druta-ui.exe` and `Verify-Druta.cmd` at the roots of those respective directories. Both run without Python. If only Git is available for the transfer, the [build instructions below](#build-from-a-fresh-checkout) produce both from source on a modern Windows build machine.

## Run once in each guest

Open the VM’s interactive graphical console and leave the guest desktop unlocked. Close any other Druta window in that guest. The GUI test draws both skins, resizes its Direct3D 9 window, and saves frames. A working virtual display adapter is sufficient. The probe reports the driver capabilities visible to that guest.

| Guest | Command to run in the guest’s Command Prompt |
|---|---|
| Windows 7 SP1 x64 | `C:\Druta-check\Verify-Druta.cmd "C:\Druta" win7` |
| Windows Vista SP2 x64 | `C:\Druta-check\Verify-Druta.cmd "C:\Druta" vista` |
| Windows XP Professional x64 Edition SP2 | `C:\Druta-check\Verify-Druta.cmd "C:\Druta" xp` |

Allow about a minute. Copy the entire directory printed as `Evidence directory` from `%TEMP%\Druta-legacy-check-…` to the server. Keep the directories separate, for example `win7-sp1-x64`, `vista-sp2-x64`, and `xp-sp2-x64`. If a run stops early, keep the partial directory and record the exact error dialog or console message.

## Decide the result

| Evidence | What to check |
|---|---|
| `results.txt` and `hashes.txt` | The requested workstation OS and service pack match, `runner_arch=AMD64`, all four `hash_…` entries say `match`, and `os_exit`, `abi_exit`, `probe_exit`, `worker_exit`, `gui_exit`, and `overall_exit` are all `0`. |
| `abi.txt`, `probe.txt`, `worker.txt` | The packaged DLL reports `core_abi=2`. The read-only probe completes. The CUDA helper reports `child_exit=2`, its expected exit when started without arguments. This checks that Windows loaded the helper. |
| `gui/smoke.json` and its BMPs | The report has `live_mode: false`, `passed: true`, and 23 passing phases. Expect 21 BMPs. Open `01-focused.bmp` and `08-classic.bmp` and confirm both interfaces have readable text and controls. |

For each guest, report the OS build and service pack, virtual display adapter and driver, application ZIP SHA-256, pass/fail for each component, and the evidence directory. Record missing NVIDIA or CUDA capabilities separately. An absent physical NVIDIA GPU does not prevent a GUI pass. A missing DLL, blank frame or incomplete report leaves that part unverified. Capture the failing filename, API name or phase before changing the guest and label any retry separately.

## Build from a fresh checkout

Use this route when the tested ZIPs cannot be transferred. A rebuild identifies the source revision, but timestamps and toolchain versions can change the archive digest. Record the rebuilt ZIP’s own SHA-256 and distribute that same ZIP to every guest.

Run the following in an **x64 Native Tools Command Prompt for Visual Studio 2022** with the Windows SDK, Git, CMake, Ninja and Python 3 available. The checkout contains this handoff and the [guest checker source](guest-check.c) and its [command script](Verify-Druta.cmd). The separate worktree pins the application to the merge that was tested.

```bat
git clone --branch docs/legacy-x64-vm-handoff https://github.com/Thermetery-Technology-Co-Limited/Druta.git Druta-handoff
cd Druta-handoff
git worktree add --detach ..\Druta-b7febbc b7febbc2bf28408f87ef4adc89465f3bead839e4
cmake -S ..\Druta-b7febbc\native -B ..\Druta-legacy-build -G Ninja -DCMAKE_BUILD_TYPE=Release -DDRUTA_BUILD_UI=ON -DDRUTA_LEGACY_X64=ON
cmake --build ..\Druta-legacy-build --target druta-legacy-x64-package
cd native\ui\validation\legacy-x64-vm
cl /nologo /O1 /Oi /GS- /Zl /W4 /TC guest-check.c /Fe:guest-check.exe /link /nologo /nodefaultlib /entry:entry /subsystem:console,5.02 /machine:x64 /manifest:no kernel32.lib user32.lib advapi32.lib
```

The application ZIP is `Druta-native-legacy-x64-Release.zip` in `Druta-legacy-build`. Extract it to `C:\Druta` in each guest. Copy `guest-check.exe` and the adjacent [Verify-Druta.cmd](Verify-Druta.cmd) to `C:\Druta-check`, then run the matching guest command above. The native build checks the pinned YY-Thunks archive and writes the import reports shipped in the ZIP. [Build dependencies and offline YY-Thunks option](../../README.md#standalone-compatibility-on-older-windows)
