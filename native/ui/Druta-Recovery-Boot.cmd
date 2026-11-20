@echo off
rem Copyright (C) 2026 Thermetery Technology Co Limited
rem SPDX-License-Identifier: GPL-3.0-or-later
setlocal DisableDelayedExpansion
rem Run this script as administrator, under the account that reviewed the clock edit.
rem The dialog identifies the GPU and receipt and asks before changing anything.
rem
rem Setup registers Druta's one-time, same-user Safe Mode logon recovery.
rem It uses Windows BCDEdit to copy the current Windows loader, sets safeboot
rem minimal on the copy, and selects only that copy for the next boot.
rem The native helper validates each change. Its BCDEdit operations are:
rem   /copy {current} /d "Druta clock recovery RECEIPT"
rem   /displayorder {copy} /remove       (if listed in the normal menu)
rem   /set {copy} safeboot minimal
rem   /set {copy} safebootalternateshell No
rem   /bootsequence {copy} /addfirst
rem Here {copy} is the identifier Windows assigns to this receipt's loader.
rem Cleanup removes only that selection and its owned copy.
rem The normal Windows loader stays the default. Boot code and BootExecute
rem are untouched. Boot changes can prompt BitLocker for the recovery key.
rem
rem After a freeze, sign in to Safe Mode with the original administrator
rem account using its password. Druta checks the protected receipt and removes matching staged
rem clock records. Restart normally after the recovery result confirms cleanup.
rem If cleanup fails or elevation is declined, stay in Safe Mode and follow the
rem retry instructions. You can also open an administrator command prompt in
rem the original Druta folder and run druta-ui.exe --recover-clock-overrides.
rem Run this script again to manage an armed recovery.
if not exist "%~dp0druta-ui.exe" (
    echo Keep this script beside druta-ui.exe in the extracted Druta package.
    pause
    exit /b 1
)
"%~dp0druta-ui.exe" --manage-clock-recovery-boot
exit /b %errorlevel%
