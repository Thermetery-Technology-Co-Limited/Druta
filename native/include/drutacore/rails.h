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

/* _refresh_volt_rail_capabilities' reset of the four caches (_volt_rail_write_error is the writer's).
 * The reference and the first-read record (increment R3) are kept, as Python keeps them. */
DC_API dc_status DC_CALL dc_rails_refresh(dc_gpu* handle);

/* ---- Increment R3: the reference capture and the readers over it --------------------------------
 *
 * Each dc_gpu also keeps _volt_rail_reference (rail -> {"bases": {field: mV}}) and
 * _volt_rail_initial_uv (rail -> the four control deltas as ints, round(delta * 1000)), filled by
 * dc_rails_limits when a rail's two paired samples agree on the paired adapter's supported
 * architecture (_VOLT_RAIL_ARCHITECTURES: NVML 4, 6, 7, 8, 10), and never cleared. They are
 * estimates for this adapter, read at the operating point of the capture: the absolute getter is
 * VID-quantized, so a base is a "first-read estimate", never a factory default. Fields arrays are
 * in VOLT_LIMIT_FIELDS order: reliability, alt_reliability, overvoltage, vmin. The architecture is
 * read as dc_gpu_architecture reads it (cached once recognized), and only where Python calls
 * self.arch(). Same locking and refusals as above; *out is untouched on DC_E_ARGUMENT. */

#define DC_RAILS_LIMIT_FIELDS 4u
#define DC_RAILS_LIMIT_MIN_MV 650.0  /* VOLT_LIMIT_MIN_MV: a request bound, not a measurement */
#define DC_RAILS_LIMIT_MAX_MV 1200.0 /* VOLT_LIMIT_MAX_MV: the same */

/* One row of read_volt_rail_limits(). The binding adds "_headroom_mv" (None: the reference holds
 * no headroom yet) and "_reference_kind" ("first-read estimate (driver status is quantized)"). */
typedef struct dc_rails_limit_row {
    int32_t type;           /* "type": the control record's first int32 */
    int32_t has_base;       /* 1: "_base_mv" is the reference's bases; 0: it is {} */
    double fields[4];       /* "reliability" .. "vmin": the signed deltas in mV */
    double base[4];         /* "_base_mv"[field] when has_base */
    int32_t absolute_ok;    /* "_absolute_ok": the rail was in this read's read_volt_rail_state */
    int32_t has_effective;  /* 0: "_effective_mv" is None */
    double boost_mv;        /* "_boost_mv": the state's, else 0.0 */
    double effective_mv;    /* "_effective_mv" when has_effective: the state's "effective" */
} dc_rails_limit_row;

/* read_volt_rail_limits(): {rail: row}; present 0 is Python's None. */
typedef struct dc_rails_limit_set {
    uint32_t size;    /* sizeof(dc_rails_limit_set), set by the caller */
    uint32_t present; /* bit r set: rails[r] is the row of rail r */
    dc_rails_limit_row rails[2];
} dc_rails_limit_set;

/* _volt_rail_profile(): has_profile 0 is None (the architecture is unknown or not supported). The
 * dicts keyed by rail ("bases", "headroom_mv", "poweron", "fields") list the rails of order[0 ..
 * order_count) in that order (the reference and the first-read record gain a rail together).
 * "headroom_mv" is None for every referenced rail and "fields" is VOLT_LIMIT_FIELDS for each. */
typedef struct dc_rails_profile_set {
    uint32_t size;          /* sizeof(dc_rails_profile_set), set by the caller */
    int32_t has_profile;
    int32_t has_reference[2]; /* _volt_rail_reference has the rail */
    double bases[2][4];       /* "bases"[rail]: _volt_rail_reference[rail]["bases"] */
    int32_t has_initial[2];   /* _volt_rail_initial_uv has the rail */
    int32_t initial[2][4];    /* "poweron"[rail]: _volt_rail_initial_uv[rail], microvolts */
    uint32_t order_count;     /* 0 .. 2 */
    uint32_t order[2];        /* the rails in insertion order */
    uint32_t control_version; /* "control_version": the version VoltRailsCtlGet last answered at, else 0x00020AC8 */
    int32_t settle_vmin;      /* "settle_vmin": architecture Pascal (4) and control_version 0x00010AC8 */
    double min_mv;            /* "min_mv": VOLT_LIMIT_MIN_MV */
    double max_mv;            /* "max_mv": VOLT_LIMIT_MAX_MV */
} dc_rails_profile_set;

/* The per-rail reason codes of volt_rail_diagnostics, in precedence order. */
#define DC_RAILS_REASON_NONE 0                 /* "" */
#define DC_RAILS_REASON_NO_ARCHITECTURE 1      /* "GPU architecture could not be detected. Retry detection." */
#define DC_RAILS_REASON_UNKNOWN_ARCHITECTURE 2 /* "Rail limit control is not yet understood for this GPU architecture." */
#define DC_RAILS_REASON_NO_CONTROL 3           /* "The driver did not return this rail's control settings. Retry detection." */
#define DC_RAILS_REASON_INVALID_STATE 4        /* "The driver did not return a valid rail status. Retry detection." */
#define DC_RAILS_REASON_NO_REFERENCE 5         /* "Waiting for stable voltage readings to establish this rail's reference." */

typedef struct dc_rails_diagnostic_rail {
    int32_t present;    /* the rail is a key of "rails": 0, or in read_volt_rail_limits or the state */
    int32_t available;  /* "available": reason_code is DC_RAILS_REASON_NONE */
    int32_t reason_code;
    char reason[128];   /* "reason": the text, NUL-terminated */
} dc_rails_diagnostic_rail;

/* volt_rail_diagnostics(), the parts the core owns: arch(), then read_volt_rail_limits(), then
 * read_volt_rail_state(). The binding adds "adapter" (from the identity), "getters"
 * (dc_rails_caches_get), and the writer's "last_write_error", "observed_packets" and
 * "write_protocol" (not ported yet). */
typedef struct dc_rails_diagnosis {
    uint32_t size;               /* sizeof(dc_rails_diagnosis), set by the caller */
    int32_t has_architecture;    /* 0: "architecture" is None */
    uint32_t architecture;       /* "architecture": NVML's enum */
    dc_rails_diagnostic_rail rails[2]; /* "rails" */
    int32_t available;           /* "available": any rail available */
    int32_t reason_code;         /* "reason": none when available, else rail 0's */
    char reason[128];
    int32_t has_architecture_source; /* 0: "architecture_source" is None (never recognized) */
    char architecture_source[32];    /* _arch_source: "NVML" or "NVAPI V<revision>" */
} dc_rails_diagnosis;

/* read_volt_rail_limits(): deltas, state, then (only when some rail read in both lacks a reference
 * and has a valid state, and the architecture is supported) a second deltas and state; a rail whose
 * second sample equals the first in the whole control row and in the state's four fields, boost and
 * type is referenced with bases[k] = absolute[k] - delta[k] (minus the boost for reliability), and
 * its first-read record is set unless it exists. When VoltRailsCtlGet last answered at 0x00010AC8,
 * rails absent from the first state are dropped. */
DC_API dc_status DC_CALL dc_rails_limits(dc_gpu* handle, dc_rails_limit_set* out);

/* _volt_rail_profile(): reads the architecture only. */
DC_API dc_status DC_CALL dc_rails_profile(dc_gpu* handle, dc_rails_profile_set* out);

DC_API dc_status DC_CALL dc_rails_diagnostics(dc_gpu* handle, dc_rails_diagnosis* out);

/* volt_rail_limits_supported(rail): has_rail 0 is rail=None (any rail supported). *out 1 or 0.
 * The profile check comes first (no getter read when it is None). A rail other than 0 and 1 is
 * never supported. */
DC_API dc_status DC_CALL dc_rails_limits_supported(dc_gpu* handle, int32_t has_rail, int32_t rail, int32_t* out);

/* volt_rail_limit_fields(rail): *count is 4 (VOLT_LIMIT_FIELDS) or 0. Reads the limits first while
 * no rail has a reference. */
DC_API dc_status DC_CALL dc_rails_limit_fields(dc_gpu* handle, int32_t rail, uint32_t* count);

/* stock_limit_mv(rail, key), field the key's VOLT_LIMIT_FIELDS index. Reads the limits first while
 * no rail has a reference. *has: 0 None (no profile, or the rail has no first-read record), 1 *mv is
 * bases[rail][field] + initial[rail][field] / 1000.0, 2 field is not 0..3 (Python's KeyError from
 * bases[rail][key], raised after the profile check). *mv is 0.0 unless *has is 1. */
DC_API dc_status DC_CALL dc_rails_stock_limit_mv(dc_gpu* handle, int32_t rail, int32_t field, int32_t* has, double* mv);

/* volt_rail_limits_mv(): {rail: {field: abs_limit_mv(row, field)}}, present 0 is None. A field
 * whose rail has no reference is NaN (the base it adds is absent). */
typedef struct dc_rails_limits_mv_set {
    uint32_t size;    /* sizeof(dc_rails_limits_mv_set), set by the caller */
    uint32_t present; /* bit r set: mv[r] is rail r's */
    double mv[2][4];
} dc_rails_limits_mv_set;

DC_API dc_status DC_CALL dc_rails_limits_mv(dc_gpu* handle, dc_rails_limits_mv_set* out);

/* ---- Increment R4: the rail write sequence and its verification ---------------------------------
 *
 * nvbackend.GPU._write_rail_records, the preconditions of _write_rail_records_locked, and
 * _verify_rail_records. The locked write itself is dc_rail_write_locked's (drutacore.h, which includes
 * this header after it), and the outcome below embeds its dc_rail_write_result. The settle decision
 * comes from the profile's settle_vmin only; no driver, board or VBIOS identity is consulted. */

#define DC_RAILS_VALUES_LIST 0  /* the record's values are a list */
#define DC_RAILS_VALUES_TUPLE 1 /* a tuple: never equal to the read-back list; printed as one */

/* The kind of a record's dict key (which equals rail 0 or 1). */
#define DC_RAILS_KEY_PLAIN 0    /* a plain int */
#define DC_RAILS_KEY_INT 1      /* an int that is not plain (a bool): `1 << key` works, prepare_write refuses it */
#define DC_RAILS_KEY_NO_SHIFT 2 /* `1 << key` raises TypeError (a float, Decimal or Fraction equal to the rail) */

/* One entry of Python's `records` dict whose key equals rail 0 or 1. */
typedef struct dc_rails_record {
    int32_t rail;       /* 0 or 1 */
    int32_t key_kind;   /* DC_RAILS_KEY_PLAIN, DC_RAILS_KEY_INT or DC_RAILS_KEY_NO_SHIFT */
    uint32_t count;     /* len(values); dc_rails_verify_records refuses a count above 4 */
    int32_t kind;       /* DC_RAILS_VALUES_LIST or DC_RAILS_VALUES_TUPLE */
    int32_t exact;      /* count is 4 and every value is a plain int in the int32 range */
    int32_t reserved;
    int64_t values[4];  /* the first min(count, 4) values; a value equal to no integer is INT64_MIN,
                           an integer outside int64 is clamped (both are outside int32, as the
                           comparisons need) */
} dc_rails_record;

/* Python's `records`: the rail entries in the dict's order, and whether it also holds a key that
 * names no rail (such a key fails every membership test: the profile's fields, the read-back). */
typedef struct dc_rails_records {
    uint32_t size;    /* sizeof(dc_rails_records), set by the caller */
    uint32_t count;   /* 0 .. 2, rails distinct */
    int32_t foreign;
    int32_t reserved;
    dc_rails_record records[2];
} dc_rails_records;

/* _write_rail_records(records): (ok, status) and the writer's three attributes. */
typedef struct dc_rails_write_outcome {
    uint32_t size;       /* sizeof(dc_rails_write_outcome), set by the caller */
    int32_t ok;
    int32_t has_status;  /* 0: Python's None */
    uint32_t status;
    /* 0: no locked write got past its preconditions; _volt_rail_write_error is None (cleared by the
     * sequence) and _volt_rail_write_protocol / _volt_rail_observed_packets are untouched.
     * 1: the fields below are those of the last locked write that ran, and set all three. */
    int32_t attributes;
    uint32_t writes;     /* locked writes that ran past their preconditions: 0, 1 or 2 */
    /* 1: Python raises TypeError at `rail_mask = sum(1 << rail ...)` because a DC_RAILS_KEY_NO_SHIFT
     * key reached it (after the boost read; nothing written, attributes 0, ok and status 0). */
    int32_t type_error;
    int32_t reserved;
    dc_rail_write_result result; /* when attributes is 1: the last locked write's (its size set by the core) */
} dc_rails_write_outcome;

/* Under the handle lock, then the process-wide rail hook mutex (GPU._RAIL_HOOK_LOCK) for the whole
 * sequence: read_volt_rail_limits (the pre-write snapshot), _volt_rail_profile; when settle_vmin,
 * settle is any record of 4 values whose rail is in the snapshot with round(vmin * 1000) differing
 * from values[3], and then the boost is read. The locked write: volt_rail_limits_supported(None),
 * the profile, every rail referenced, volt_rail_limits_supported(rail) per rail in order, 4 values
 * each, the boost read; any refusal is (False, None) with attributes 0. A DC_RAILS_KEY_NO_SHIFT key
 * then stops the sequence with type_error 1. A DC_RAILS_KEY_INT key, or a record that is not exact,
 * is refused with prepare_write's text and no escape (Python refuses it inside the hook, after the
 * GET). Otherwise dc_rail_write_locked's write. When
 * settle and that gave (True, 0): the verification of dc_rails_verify_records with the boost read
 * before the write; a failure is (False, 0) with the first write's attributes; else the locked
 * write runs again and its result is returned. DC_E_ARGUMENT for a NULL argument, a size mismatch,
 * count > 2, a rail other than 0 or 1, a repeated rail, or a value kind or key kind outside its
 * defines. */
DC_API dc_status DC_CALL dc_rails_write_records(dc_gpu* handle, const dc_rails_records* records,
                                                dc_rails_write_outcome* out);

/* _verify_rail_records(records, boost): has_back 1 with back (read_volt_rail_limits) and an empty
 * error, or has_back 0 and error "write issued but the rail read-back is incomplete", "rail R
 * read-back disagrees: wanted W, got G" (W as Python prints the list or tuple, G the list of
 * int(round(field * 1000))) or "rail write changed the voltage boost setting". has_boost 0 is a
 * boost of None. Handle lock only (Python takes no hook lock here). DC_E_ARGUMENT as for
 * dc_rails_write_records, and also for a record count above 4 (`wanted` is printed from the four
 * values carried). */
typedef struct dc_rails_verify_result {
    uint32_t size;    /* sizeof(dc_rails_verify_result), set by the caller */
    int32_t has_back;
    char error[256];
    dc_rails_limit_set back; /* its size is set by the core */
} dc_rails_verify_result;

DC_API dc_status DC_CALL dc_rails_verify_records(dc_gpu* handle, const dc_rails_records* records, int32_t has_boost,
                                                 int32_t boost, dc_rails_verify_result* out);

#ifdef __cplusplus
}
#endif

#endif /* DRUTACORE_RAILS_H */
