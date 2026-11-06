@echo off
rem Druta diagnostics collector for Windows 7 SP1 and Vista SP2 (x64).
rem
rem Run this from the extracted Druta folder (double-click is fine). It is
rem read-only: it changes no Windows, driver or GPU setting. It writes a
rem Druta-diagnostics-<time> folder with:
rem   summary.txt         every finding in one place
rem   imports-check.txt   each DLL the bundle needs from Windows, and whether
rem                       this machine has it (names the "missing DLL" cause
rem                       of a loader error before Python starts)
rem   os.txt, hotfixes.txt, gpu.txt, system-dlls.txt, bundle-files.txt
rem   self-check.txt      Druta's own probes, if Druta.exe can start
rem   logs\               Druta's session logs, including crash stacks
rem Send the whole folder back. User and computer names are not collected.
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"

set "SYS=%SystemRoot%\System32"
rem A 32-bit cmd.exe on 64-bit Windows sees SysWOW64 as System32.
if defined PROCESSOR_ARCHITEW6432 set "SYS=%SystemRoot%\Sysnative"

set "STAMP="
for /f "skip=1 tokens=1" %%t in ('wmic os get localdatetime 2^>nul') do if not defined STAMP set "STAMP=%%t"
if defined STAMP (set "STAMP=%STAMP:~0,8%-%STAMP:~8,6%") else set "STAMP=%RANDOM%%RANDOM%"
set "OUT=%~dp0Druta-diagnostics-%STAMP%"
mkdir "%OUT%" 2>nul
if not exist "%OUT%\" set "OUT=%TEMP%\Druta-diagnostics-%STAMP%"
mkdir "%OUT%" 2>nul
if not exist "%OUT%\" (
  echo Could not create a diagnostics folder.
  pause
  exit /b 1
)
set "SUM=%OUT%\summary.txt"
echo Collecting Druta diagnostics...

> "%SUM%" echo Druta diagnostics %STAMP%
>>"%SUM%" echo.

rem ---- Windows version -------------------------------------------------------
> "%OUT%\os.txt" ver
rem wmic writes UTF-16 to a file; piping through MORE converts it to text findstr can read.
wmic os get Caption,Version,BuildNumber,CSDVersion,OSArchitecture /format:list 2>&1 | more >>"%OUT%\os.txt"
>>"%OUT%\os.txt" echo PROCESSOR_ARCHITECTURE=%PROCESSOR_ARCHITECTURE%
>>"%OUT%\os.txt" echo PROCESSOR_ARCHITEW6432=%PROCESSOR_ARCHITEW6432%
>>"%SUM%" echo == Windows
for /f "tokens=*" %%l in ('ver') do >>"%SUM%" echo %%l
>>"%SUM%" echo.

rem ---- Updates the legacy builds depend on -----------------------------------
wmic qfe get HotFixID,InstalledOn 2>&1 | more > "%OUT%\hotfixes.txt"
>>"%SUM%" echo == Updates (informational; see WINDOWS7.md / VISTA.md)
call :kb KB2533623 "Windows 7 loader update needed by CPython extension loading, or a superseding update such as KB3063858"
call :kb KB3063858 "Windows 7 loader security update that supersedes KB2533623"
call :kb KB2999226 "Universal C Runtime; the portable build carries its own"
call :kb KB2670838 "Windows 7 Platform Update, Direct3D 11.1"
call :kb KB971644 "Vista Platform Update, Direct3D 11"
>>"%SUM%" echo.

rem ---- GPU and driver --------------------------------------------------------
wmic path Win32_VideoController get Name,DriverVersion,DriverDate,PNPDeviceID,VideoProcessor /format:list 2>&1 | more > "%OUT%\gpu.txt"
>>"%SUM%" echo == Display adapters
for /f "tokens=1,* delims==" %%a in ('wmic path Win32_VideoController get Name^,DriverVersion /format:list 2^>nul ^| findstr "="') do >>"%SUM%" echo %%a=%%b
>>"%SUM%" echo.

rem ---- System libraries Druta uses at run time -------------------------------
> "%OUT%\system-dlls.txt" echo System directory checked: %%SystemRoot%%\System32 (64-bit view)
>>"%SUM%" echo == System libraries
for %%d in (d3d11.dll dxgi.dll dwmapi.dll d3dcompiler_47.dll ucrtbase.dll vcruntime140.dll msvcp140.dll nvapi64.dll nvml.dll nvcuda.dll) do call :sysdll %%d
if exist "%ProgramFiles%\NVIDIA Corporation\NVSMI\nvml.dll" (
  >>"%OUT%\system-dlls.txt" echo nvml.dll: present in NVIDIA Corporation\NVSMI
  >>"%SUM%" echo nvml.dll: present in NVIDIA Corporation\NVSMI
)
>>"%SUM%" echo.

rem ---- Every DLL the bundle expects from Windows ------------------------------
>>"%SUM%" echo == Bundle imports (from Druta-imports.txt)
if exist "%~dp0Druta-imports.txt" (
  > "%OUT%\imports-check.txt" echo dll: result - importers
  set /a MISSING=0
  for /f "usebackq eol=# tokens=1,2 delims=|" %%a in ("%~dp0Druta-imports.txt") do call :import "%%a" "%%b"
  call :importsummary
) else (
  >>"%SUM%" echo Druta-imports.txt is not in this folder; the import check was skipped.
)
>>"%SUM%" echo.

rem ---- Bundle contents (relative paths and sizes only) -----------------------
forfiles /p "%~dp0." /s /c "cmd /c if @isdir==FALSE echo @relpath @fsize" 2>nul | findstr /v /i /c:"Druta-diagnostics-" > "%OUT%\bundle-files.txt"

rem ---- Druta's own self-check ---------------------------------------------------
>>"%SUM%" echo == Druta self-check
if exist "%~dp0Druta.exe" (
  echo Starting Druta.exe --self-check. If Windows shows an error dialog,
  echo note its text, press OK, and this script will continue.
  start "" /wait "%~dp0Druta.exe" --self-check --output "%OUT%"
  call :exitcode
) else (
  >>"%SUM%" echo Druta.exe is not in this folder.
)
if exist "%OUT%\self-check.txt" (
  findstr /b /c:"-> FAILED" /c:"FAILED " /c:"self-check finished" "%OUT%\self-check.txt" >>"%SUM%"
) else (
  >>"%SUM%" echo No self-check report was written: Druta.exe could not start Python.
  >>"%SUM%" echo The "Bundle imports" section above lists the missing DLLs.
)
>>"%SUM%" echo.

rem ---- Session logs ------------------------------------------------------------
mkdir "%OUT%\logs" 2>nul
if exist "%~dp0logs\druta-*.log" copy /y "%~dp0logs\druta-*.log" "%OUT%\logs\" >nul 2>&1
if defined LOCALAPPDATA if exist "%LOCALAPPDATA%\Thermetery\Druta\logs\druta-*.log" copy /y "%LOCALAPPDATA%\Thermetery\Druta\logs\druta-*.log" "%OUT%\logs\" >nul 2>&1
if exist "%TEMP%\Druta-logs\druta-*.log" copy /y "%TEMP%\Druta-logs\druta-*.log" "%OUT%\logs\" >nul 2>&1

echo.
echo Done. Send this folder back (right-click, Send to, Compressed folder):
echo   %OUT%
echo.
type "%SUM%"
pause
exit /b 0

rem ---- helpers -------------------------------------------------------------------
:kb
rem No parenthesized blocks here: a ")" in the description would close one.
set "KBSTATE=installed"
findstr /i /c:"%~1" "%OUT%\hotfixes.txt" >nul 2>&1 || set "KBSTATE=not installed"
>>"%SUM%" echo %~1 %KBSTATE% - %~2
exit /b 0

:sysdll
set "WHERE=missing"
if exist "%SYS%\%~1" set "WHERE=present in System32"
if exist "%~dp0%~1" set "WHERE=%WHERE%; bundled"
if exist "%~dp0_internal\%~1" set "WHERE=%WHERE%; bundled in _internal"
>>"%OUT%\system-dlls.txt" echo %~1: %WHERE%
>>"%SUM%" echo %~1: %WHERE%
exit /b 0

:import
set "RESULT=MISSING"
if exist "%SYS%\%~1" set "RESULT=present in System32"
if "%RESULT%"=="MISSING" if exist "%SYS%\downlevel\%~1" set "RESULT=present in System32\downlevel"
if "%RESULT%"=="MISSING" if exist "%~dp0%~1" set "RESULT=present beside Druta.exe"
>>"%OUT%\imports-check.txt" echo %~1: %RESULT% - %~2
if not "%RESULT%"=="MISSING" exit /b 0
set /a MISSING+=1
>>"%SUM%" echo MISSING %~1 - needed by %~2
exit /b 0

:importsummary
>>"%SUM%" echo %MISSING% DLL(s) the bundle needs were not found on this machine.
exit /b 0

:exitcode
>>"%SUM%" echo Druta.exe --self-check exit code: %ERRORLEVEL%
exit /b 0
