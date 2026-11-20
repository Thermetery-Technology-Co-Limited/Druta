# Druta 1.9.3 native prerelease

This prerelease supersedes 1.9.2. Extract the native x64 ZIP and run
`druta-ui.exe`. The guest-check ZIP contains the legacy Windows validation utility.

- The XOC board-power slider reaches 1,999 W on Ampere adapters that expose the
  extended RM26 layout and pass Druta's power-policy and request-readback checks.
  Druta still uses the adapter's normal board-power range with XOC off.
- The Classic header, About dialog and selected-device report display 1.9.3.

On driver 472.12, Druta read the same extended RM26 layout on the RTX 3070 Ti
and GTX 1050 Ti. The driver reported normal ranges of 100–380 W and 66–113 W
respectively. Both adapters exposed a 1,999 W software request ceiling in XOC.
At idle, the RTX 3070 Ti accepted and read back a 99 W XOC request, then
returned to 310 W with identical client and native policy state. Firmware and
other on-card constraints still determine actual power.
