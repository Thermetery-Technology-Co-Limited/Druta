# Focused routing and shunt helper actions — 2026-11-12

Additional memory/VRAM offset is now in Focused Advanced. NVVDD and MSVDD
offsets are in Rails. Crossbar, SYS, Video and LTC remain in Daily. All routes
reuse the existing draft identities, XOC/lock gates and command paths; Classic
grouping is unchanged.

The parallel-resistance helper adds **Use for all rails** and **Copy value**.
Use for all rails changes only understood rails' effective resistances,
preserving original values, layout and unknown entries. Copy value copies the
numeric result in mOhm with nine significant digits. Invalid calculations
disable both actions. Neither action saves a file or writes hardware.

The Release build and bounded PE/import audit passed. All 29 CTest suites
passed, including 923 UI-view checks, 307 Advanced-view checks, 92 shunt-view
checks and 143 session checks. Tests activate the actual helper buttons with a
mock clipboard, verify 5 and 8 mOhm in parallel as 40/13 mOhm, preserve unknown
metadata/original resistances, and check invalid/no-known-rail behavior.
Relocated rows appear only on their assigned Focused page; drafts and gates
survive navigation.

The initial full run exposed a pre-existing test race: the fake maintenance
counter advanced before its error log was published. The test now deliberately
blocks that boundary, waits for publication, and verifies that subsequent
maintenance neither polls ordinary telemetry nor duplicates the log. Production
maintenance code was unchanged. The full rerun passed.

Read-only native renderer captures of Daily, Advanced and Rails exited with
code 0 and were visually reviewed on the owner's RTX 3070 Ti. No tuning write,
OS clipboard modification or global shunt-config Save was performed for this
validation. The clipboard check uses the injected ImGui test callback.

Final application SHA-256:
`5B795E83F312C468FE9F9E39FFB56CBE32A31A2D849A70C5D9A08D2BA3FFD644`.
The backend DLL is unchanged from the preceding checkpoint.

The separate [Classic / Verbose review](2026-11-12-verbose-parity-review.md)
and [Focused inventory](2026-11-12-focused-port-inventory.md) compare Python
`fa6ee83` against native `d5034e8` and identify remaining port work. They do not
claim that visual/backend parity gaps have already been fixed.
