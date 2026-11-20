@echo off
setlocal EnableExtensions DisableDelayedExpansion
if "%~1"=="" goto :usage
if "%~2"=="" goto :usage
if not "%~3"=="" goto :usage
set "DRUTA_VERIFY_OS=%~2"
if /i "%DRUTA_VERIFY_OS%"=="win7" goto :argsok
if /i "%DRUTA_VERIFY_OS%"=="vista" goto :argsok
if /i "%DRUTA_VERIFY_OS%"=="xp" goto :argsok
if /i "%DRUTA_VERIFY_OS%"=="host" goto :argsok
goto :usage
:argsok
set "DRUTA_RUNNER=%~dp0guest-check.exe"
if not exist "%DRUTA_RUNNER%" goto :missing
pushd "%~1" || goto :missing
for %%F in (druta-ui.exe drutacore.dll druta-probe.exe druta-ui-timing-cuda.exe imports.json core-imports.json probe-imports.json cuda-imports.json) do if not exist "%%F" goto :badpackage
set "DRUTA_VERIFY_OUTPUT=%TEMP%\Druta-legacy-check-%RANDOM%-%RANDOM%"
if exist "%DRUTA_VERIFY_OUTPUT%" goto :badoutput
mkdir "%DRUTA_VERIFY_OUTPUT%" || goto :badoutput
mkdir "%DRUTA_VERIFY_OUTPUT%\gui" || goto :badoutput
set "DRUTA_RESULT=%DRUTA_VERIFY_OUTPUT%\results.txt"
>"%DRUTA_RESULT%" echo Guest target %DRUTA_VERIFY_OS%
>>"%DRUTA_RESULT%" ver
set "DRUTA_VERIFY_ACTION=os"
"%DRUTA_RUNNER%" >>"%DRUTA_RESULT%" 2>&1
set "DRUTA_OS_RC=%errorlevel%"
>>"%DRUTA_RESULT%" echo os_exit=%DRUTA_OS_RC%
if not "%DRUTA_OS_RC%"=="0" goto :wrongos
set "DRUTA_FAIL=0"
echo Test results will be in "%DRUTA_VERIFY_OUTPUT%".
set "DRUTA_VERIFY_ACTION=hash"
"%DRUTA_RUNNER%" >"%DRUTA_VERIFY_OUTPUT%\hashes.txt" 2>&1
if errorlevel 1 set "DRUTA_FAIL=1"
call :checkhash druta-ui.exe imports.json
call :checkhash drutacore.dll core-imports.json
call :checkhash druta-probe.exe probe-imports.json
call :checkhash druta-ui-timing-cuda.exe cuda-imports.json
set "DRUTA_VERIFY_ACTION=abi"
call :run
set "DRUTA_VERIFY_ACTION=probe"
call :run
set "DRUTA_VERIFY_ACTION=worker"
call :run
set "DRUTA_VERIFY_ACTION=gui"
call :run
if not exist "%DRUTA_VERIFY_OUTPUT%\gui\smoke.json" set "DRUTA_FAIL=1"
for %%F in (01-focused.bmp 08-classic.bmp 23-power-weights.bmp) do if not exist "%DRUTA_VERIFY_OUTPUT%\gui\%%F" set "DRUTA_FAIL=1"
>>"%DRUTA_RESULT%" echo overall_exit=%DRUTA_FAIL%
echo Evidence directory: "%DRUTA_VERIFY_OUTPUT%"
type "%DRUTA_RESULT%"
popd
exit /b %DRUTA_FAIL%
:run
"%DRUTA_RUNNER%" >"%DRUTA_VERIFY_OUTPUT%\%DRUTA_VERIFY_ACTION%.txt" 2>&1
set "DRUTA_RC=%errorlevel%"
if not "%DRUTA_RC%"=="0" set "DRUTA_FAIL=1"
>>"%DRUTA_RESULT%" echo %DRUTA_VERIFY_ACTION%_exit=%DRUTA_RC%
exit /b 0
:checkhash
set "DRUTA_HASH="
for /f "usebackq tokens=1,2,3" %%A in ("%DRUTA_VERIFY_OUTPUT%\hashes.txt") do if /i "%%A"=="%~1" if /i "%%B"=="SHA256" set "DRUTA_HASH=%%C"
if not defined DRUTA_HASH goto :badhash
findstr /i /l /c:"%DRUTA_HASH%" "%~2" >nul || goto :badhash
>>"%DRUTA_RESULT%" echo hash_%~1=match
exit /b 0
:badhash
>>"%DRUTA_RESULT%" echo hash_%~1=unverified
set "DRUTA_FAIL=1"
exit /b 0
:wrongos
>>"%DRUTA_RESULT%" echo overall_exit=2
echo The OS identity does not match %DRUTA_VERIFY_OS%. Evidence: "%DRUTA_RESULT%"
type "%DRUTA_RESULT%"
popd
exit /b 2
:badpackage
echo The folder is missing a required package binary or import report.
popd
exit /b 2
:badoutput
echo Could not create a fresh results directory in TEMP.
popd
exit /b 2
:missing
echo Could not open the package folder or find guest-check.exe beside this script.
exit /b 2
:usage
echo Usage: Verify-Druta.cmd "C:\path to extracted package" win7^|vista^|xp
echo The host option records a development-machine control run.
exit /b 2
