/* Druta - a monitor and tuner for NVIDIA GPUs.
 * Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The voltage-rail getters (nvbackend.GPU._read_volt_rail_blocks and the readers over it): dc_rails_*.
 * Included by drutacore.h; do not include directly.
 *
 * Increment R2. Each dc_gpu keeps the four per-GPU caches of the Python original
 * (_volt_rail_masks_cache, _volt_rail_retry_after, _volt_rail_getter_diagnostics,
 * _volt_rail_read_versions) with the same semantics; dc_rails_refresh clears them as
 * _refresh_volt_rail_capabilities does. Rails are positional (0 and 1) and each is read with its
 * singleton mask 1 << rail; the combined mask is never sent. The retry cadence runs on the core's
 * monotonic clock (dc_set_clock_hook; Python's time.monotonic()).
 *
 * Every entry takes the handle lock (the GPU's RLock; Python does not wrap these getters, but the
 * caches are shared state the native writer paths also reach). Returns: DC_OK, DC_E_ARGUMENT for a
 * NULL handle or out pointer, a caller struct whose size is not this header's, or a getter that is
 * neither DC_RAILS_CONTROL nor DC_RAILS_ABS; DC_E_FAILED if a C++ exception reached the entry (only
 * allocation can throw). A driver status never fails an entry: it is recorded in the diagnostics. */
#ifndef DRUTACORE_RAILS_H
#define DRUTACORE_RAILS_H

#ifdef __cplusplus
extern "C" {
#endif

/* The getter (Python's `function` argument). */
#define DC_RAILS_CONTROL 0 /* "VoltRailsCtlGet", NVAPI interface 0xA3070DB0: signed control deltas */
#define DC_RAILS_ABS 1     /* "VoltRailsAbs", NVAPI interface 0x5D0634EE: absolute and live microvolts */
#define DC_RAILS_CONTROL_ID 0xA3070DB0u
#define DC_RAILS_ABS_ID 0x5D0634EEu
#define DC_RAILS_COUNT 2u          /* rails 0 and 1 */
#define DC_RAILS_BLOCK_SIZE 8192u  /* the zeroed buffer handed to the getter on each attempt */
#define DC_RAILS_CONTROL_VERSION 0x00020AC8u /* _read_volt_rail_deltas; 0x00010AC8 is its fallback */
#define DC_RAILS_LIVE_VERSION 0x00010AC8u    /* GPU.LIVE_RAIL_VER */

/* _read_volt_rail_blocks(function, version): {rail: buffer}. */
typedef struct dc_rails_block_set {
    uint32_t size;    /* sizeof(dc_rails_block_set), set by the caller */
    uint32_t present; /* bit r set: rail r was read, blocks[r] holds the buffer the getter filled */
    uint8_t blocks[2][8192];
} dc_rails_block_set;

/* One row of _read_volt_rail_deltas: the int32 record at (0x48 + rail * 0x54) / 4 words. */
typedef struct dc_rails_delta {
    int32_t type;           /* "type": the record's first int32 */
    int32_t reserved;
    double reliability;     /* the next four int32 / 1000.0, in VOLT_LIMIT_FIELDS order */
    double alt_reliability;
    double overvoltage;
    double vmin;
} dc_rails_delta;

/* _read_volt_rail_deltas(): {rail: row} (an empty dict is present 0). */
typedef struct dc_rails_delta_set {
    uint32_t size;    /* sizeof(dc_rails_delta_set), set by the caller */
    uint32_t present; /* bit r set: rails[r] is the row of rail r */
    dc_rails_delta rails[2];
} dc_rails_delta_set;

/* One row of read_volt_rail_state: the u32 record at (0x48 + rail * 0x54) / 4 words. */
typedef struct dc_rails_state_row {
    uint32_t type; /* "type": the raw u32 discriminator (never used as the key) */
    uint32_t reserved;
    double boost_mv; /* "_boost_mv": header word 2 / 1000.0 */
    double live;     /* the next six u32 / 1000.0, in LIVE_RAIL_FIELDS order */
    double reliability;
    double alt_reliability;
    double overvoltage;
    double effective;
    double vmin;
} dc_rails_state_row;

/* read_volt_rail_state(): {rail: row}, or None (present 0). A record whose live and reliability
 * are both zero is not reported. */
typedef struct dc_rails_state_set {
    uint32_t size;    /* sizeof(dc_rails_state_set), set by the caller */
    uint32_t present; /* bit r set: rails[r] is the row of rail r; 0 is Python's None */
    dc_rails_state_row rails[2];
} dc_rails_state_set;

/* One entry of _volt_rail_getter_diagnostics[function][rail]: the last attempt. */
typedef struct dc_rails_diagnostic {
    int32_t present;  /* 0: no such key */
    uint32_t version; /* "version": the requested version word, "0x%08X" in Python */
    /* "status": the getter's return. Python records None (and "error" str(exc)) when ctypes
     * converts a Windows structured exception inside the getter into OSError; the core, like every
     * other NVAPI call it makes, has no such guard, so a faulting export is not survived and status
     * is always an int here. */
    int32_t status;
    int32_t valid;    /* "valid": status 0 and both the version and the mask echoed */
    int32_t attempts; /* "attempts": 1 or 2 */
} dc_rails_diagnostic;

/* The four caches, indexed [function] or [function][rail]. A has_ / present flag of 0 is an absent
 * key. "error" of each diagnostic is always None natively (Python's str(exc) of a getter fault that
 * ctypes turned into OSError; see dc_rails_diagnostic.status). */
typedef struct dc_rails_caches {
    uint32_t size;          /* sizeof(dc_rails_caches), set by the caller */
    int32_t has_known[2];   /* _volt_rail_masks_cache has the function */
    uint32_t known[2];      /* its tuple of rails as a mask (bit r: rail r), sorted when rebuilt */
    int32_t has_retry[2][2];       /* _volt_rail_retry_after has (function, rail) */
    double retry_after[2][2];      /* its monotonic seconds */
    dc_rails_diagnostic diagnostics[2][2];
    int32_t has_version[2];  /* _volt_rail_read_versions has the function */
    uint32_t version[2];     /* the version that function last answered at */
} dc_rails_caches;

/* _read_volt_rail_blocks(function, version). Candidates are rails 0 and 1 already read by that
 * getter or whose retry time has passed. Versions: the remembered one first (else `version`), then
 * (0x00020AC8, 0x00010AC8) for DC_RAILS_CONTROL or `version` for DC_RAILS_ABS. Per rail and version,
 * up to two attempts: the second follows any first attempt that is neither valid nor status -9,
 * which includes a status 0 whose echoed version or mask differs. A status other than -9 after the
 * last attempt stops trying further versions; a rail not read retries no sooner than 2 s later
 * (measured from one clock sample taken before the first getter call). No call is made
 * (and nothing cached) without a ready NVAPI half and that getter: present 0. */
DC_API dc_status DC_CALL dc_rails_blocks(dc_gpu* handle, int32_t function, uint32_t version, dc_rails_block_set* out);

/* _read_volt_rail_deltas(): DC_RAILS_CONTROL at 0x00020AC8. */
DC_API dc_status DC_CALL dc_rails_deltas(dc_gpu* handle, dc_rails_delta_set* out);

/* read_volt_rail_state(): DC_RAILS_ABS at 0x00010AC8. */
DC_API dc_status DC_CALL dc_rails_state(dc_gpu* handle, dc_rails_state_set* out);

/* read_rail_live_mv(rail): one read_volt_rail_state, then *has 1 and *mv that rail's "live", or
 * *has 0 (None; any rail other than 0 and 1 is None after the read). */
DC_API dc_status DC_CALL dc_rails_live_mv(dc_gpu* handle, int32_t rail, int32_t* has, double* mv);

/* The four caches as they stand. */
DC_API dc_status DC_CALL dc_rails_caches_get(dc_gpu* handle, dc_rails_caches* out);

/* _refresh_volt_rail_capabilities' reset of the four caches (_volt_rail_write_error is the writer's). */
DC_API dc_status DC_CALL dc_rails_refresh(dc_gpu* handle);

#ifdef __cplusplus
}
#endif

#endif /* DRUTACORE_RAILS_H */
