/* Druta - a monitor and tuner for NVIDIA GPUs.
 * Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * drutacore C ABI (draft; the surface grows one feature at a time).
 *
 * Rules:
 *   - Plain C: no C++ types, no exceptions, no callbacks that unwind.
 *   - Every function returns a dc_status; outputs go through caller-owned buffers.
 *   - Strings are UTF-8, NUL-terminated; buffer sizes are in bytes and include the NUL.
 *   - A function that needs more buffer returns DC_E_BUFFER and writes the needed size.
 *   - The ABI version changes only when an existing function or struct changes meaning.
 */
#ifndef DRUTACORE_H
#define DRUTACORE_H

#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32)
#  if defined(DRUTACORE_BUILDING)
#    define DC_API __declspec(dllexport)
#  else
#    define DC_API __declspec(dllimport)
#  endif
#  define DC_CALL __cdecl
#else
#  define DC_API __attribute__((visibility("default")))
#  define DC_CALL
#endif

#ifdef __cplusplus
extern "C" {
#endif

#define DC_ABI_VERSION 2u  /* 2: dc_gpu_lock / trylock / unlock / lock_held and DC_E_STATE */

typedef int32_t dc_status;
enum {
    DC_OK = 0,
    DC_E_ARGUMENT = -1,      /* a NULL or malformed argument */
    DC_E_BUFFER = -2,        /* caller buffer too small; *needed holds the size */
    DC_E_PARSE = -3,         /* input text does not have the expected shape */
    DC_E_UNRECOGNIZED = -4,  /* packet is not an exact understood layout */
    DC_E_MASK = -5,          /* rail mask selects no rail or an unsupported one */
    DC_E_RECORDS = -6,       /* records do not match the rail mask exactly */
    DC_E_RANGE = -7,         /* a value is outside its allowed range */
    DC_E_UNAVAILABLE = -8,     /* the adapter does not provide this value (Python's None) */
    DC_E_FAILED = -9,          /* the operation failed; the dc_error passed says how */
    DC_E_CALLER = -10,         /* a caller-supplied callback reported a failure; the operation was
                                  abandoned before any write */
    DC_E_STATE = -11           /* the call does not fit the object's current state: an unlock by a
                                  thread that holds no level taken through dc_gpu_lock or
                                  dc_gpu_trylock; nothing was changed */
};

/* The ABI version this library implements (DC_ABI_VERSION of its header). */
DC_API uint32_t DC_CALL dc_abi_version(void);

/* A static, English description of a status code. */
DC_API const char* DC_CALL dc_status_text(dc_status status);

/* "0000:01:00.0" -> domain, bus, device, function. NVML's 8-digit domain is accepted. */
DC_API dc_status DC_CALL dc_parse_slot(const char* text, uint32_t* domain, uint32_t* bus,
                                       uint32_t* device, uint32_t* function);

/* domain, bus, device, function -> "0000:01:00.0" in a caller buffer of at least 13 bytes. */
DC_API dc_status DC_CALL dc_format_slot(uint32_t domain, uint32_t bus, uint32_t device, uint32_t function,
                                        char* buffer, size_t size, size_t* needed);

/* 1 when both parse and share bus and device, else 0 (never an error). */
DC_API int32_t DC_CALL dc_same_slot(const char* a, const char* b);

/* ---- voltage-rail packets (rail_transport) ---------------------------------------------------- */

typedef struct dc_rail_layout {
    uint32_t packet_size;
    uint32_t params_size;
    uint32_t get_command;
    uint32_t set_command;
    uint32_t mask_word;
    uint32_t boost_word;
    uint32_t record0;
    uint32_t record_stride;
    uint32_t record_type;
    int32_t valid_word; /* -1 when the layout has no valid word */
} dc_rail_layout;

typedef struct dc_rail_record {
    uint32_t rail; /* 0 or 1 */
    int32_t values[4];
} dc_rail_record;

/* The understood GET layouts, in the same order as rail_transport.KNOWN_LAYOUTS. */
DC_API uint32_t DC_CALL dc_rail_layout_count(void);
DC_API dc_status DC_CALL dc_rail_layout_at(uint32_t index, dc_rail_layout* layout);

/* Index of the exact understood GET layout of payload, or DC_E_UNRECOGNIZED. */
DC_API dc_status DC_CALL dc_rail_recognize(const uint8_t* payload, size_t size, uint32_t requested_mask,
                                           uint32_t* layout_index);

/* Build the SET packet (same size as payload) that changes only the owned words.
 * records holds exactly one record per rail selected in requested_mask. */
DC_API dc_status DC_CALL dc_rail_prepare_write(const uint8_t* payload, size_t size, uint32_t requested_mask,
                                               const dc_rail_record* records, size_t record_count, uint32_t boost,
                                               uint8_t* out, size_t out_size, size_t* needed);

/* A compact description for diagnostics ("rail packet size=... layout=..."). */
DC_API dc_status DC_CALL dc_rail_packet_summary(const uint8_t* payload, size_t size, char* buffer, size_t buffer_size,
                                                size_t* needed);

/* ---- GPU identity (nvbackend.Nvml / NvAPI / enumerate_gpus / GPU identity) ------------------- */

/* Test seam: serve driver exports from the caller instead of nvapi64.dll / nvml.dll.
 * resolve(context, library, name) with library "nvapi" or "nvml"; name "" asks whether the
 * library loads at all (non-NULL means yes). NULL restores the platform loader. */
typedef void*(DC_CALL* dc_resolve_fn)(void* context, const char* library, const char* name);
DC_API void DC_CALL dc_set_export_resolver(dc_resolve_fn resolve, void* context);

/* Resolve an export through the core's loader, including its test resolver. library is exactly
 * "nvapi" or "nvml"; name must be nonempty. No driver initialization or enumeration occurs here.
 * The loaded module is kept for the current resolver generation. DC_E_UNRECOGNIZED and *out NULL
 * mean the library or export is absent; DC_E_ARGUMENT is a bad argument. As with the resolver seam,
 * callers must not replace the resolver while an address returned from it is in use. */
DC_API dc_status DC_CALL dc_export_address(const char* library, const char* name, void** out);

typedef struct dc_gpu_entry {
    uint32_t size;       /* sizeof(dc_gpu_entry), set by the caller */
    char slot[24];       /* "0000:01:00.0", "" when unknown */
    char name[96];
    char uuid[96];
    int32_t has_devid;   /* 0: NVML had no versioned PCI identity (devid/subsys unset) */
    uint32_t devid;
    uint32_t subsys;
    int32_t nvml_index;  /* -1: NVML did not enumerate it */
    int32_t has_nvapi;
} dc_gpu_entry;

/* Every NVIDIA GPU on the host, ordered by PCI slot. *count receives the total even when the
 * array is too small (then DC_E_BUFFER). A fresh enumeration through both libraries each call. */
DC_API dc_status DC_CALL dc_enumerate_gpus(dc_gpu_entry* entries, size_t capacity, size_t* count);

typedef struct dc_gpu dc_gpu;

/* Open the card at slot ("" or NULL: the lowest slot). Never fails for a missing driver: the
 * handle then reports both halves unavailable. Statuses: DC_OK; DC_E_ARGUMENT for a NULL out
 * pointer; DC_E_BUFFER when the handle could not be allocated; DC_E_FAILED when building it threw
 * (no dc_error record is involved: the status is the whole report). *gpu is NULL on every failure
 * but DC_E_ARGUMENT, which does not touch it. */
DC_API dc_status DC_CALL dc_gpu_open(const char* slot, dc_gpu** gpu);
/* Free the handle. The caller guarantees that no thread is using it, holds its lock or waits for
 * it: freeing a handle another thread holds or waits on is undefined behaviour (the binding keeps
 * the handle alive until its last reference, so it never does). */
DC_API void DC_CALL dc_gpu_close(dc_gpu* gpu);

/* The handle's own mutex, the one every entry that takes "the handle lock" takes. It is recursive
 * per OS thread and stays held between calls, so a caller can hold it across a sequence of entries
 * (a call and its message fetch, a multi-call write) and each entry inside only adds a level. A
 * callback the core makes while it holds the mutex runs on the holding thread, so taking the lock
 * there is recursion, not a wait. Three entries give their own level back between their steps:
 * dc_legacy_p0_hold across each 100 ms wait, dc_perf_read_group between attempts and
 * dc_clock_bios_read between its two attempts; other threads get the mutex there only when the
 * caller holds no level of its own. Every other entry keeps its level for the whole call, waits
 * included (dc_gpu_fan_restore polls its read-back under it for up to about two seconds).
 * The core counts the levels a caller takes here apart from the levels its entries take: an unlock
 * gives back only a level taken through dc_gpu_lock / dc_gpu_trylock, so an unlock inside a core
 * callback can never release the running entry's own level. Each level a caller takes must be
 * released by the same thread, and a thread must not exit while it holds one: the mutex then stays
 * held for good, and a later thread that the operating system gives the same thread id (Windows
 * reuses ids readily; Linux reuses kernel thread ids once its counter wraps) is taken for its owner
 * by the operating system's mutex and by the core alike.
 *   dc_gpu_lock:      blocks until the calling thread holds it. DC_E_ARGUMENT for a NULL handle;
 *                     DC_E_FAILED if the platform mutex refused (nothing taken).
 *   dc_gpu_trylock:   never waits: *acquired 1 when the calling thread now holds it (one more level
 *                     when it already did), 0 when another thread holds it. DC_E_ARGUMENT for a NULL
 *                     argument.
 *   dc_gpu_unlock:    releases one level the calling thread took through dc_gpu_lock or
 *                     dc_gpu_trylock. DC_E_STATE, with nothing changed, when it holds none (another
 *                     thread holds the mutex, none does, or inside a core callback the thread holds
 *                     only the entry's level). DC_E_ARGUMENT for NULL. A caller that unlocks once too
 *                     often inside a callback while it still held a level of its own gives up that
 *                     level early: this is the caller's error, not undefined behaviour; the running
 *                     entry keeps its own level, so nothing in it runs unserialized and nothing is
 *                     left held, and the caller's later unlock of that level returns DC_E_STATE.
 *   dc_gpu_lock_held: read-only: *held 1 when the calling thread holds the mutex at any level (its
 *                     own, or a core entry's while a callback runs), else 0. DC_E_ARGUMENT for a
 *                     NULL argument. */
DC_API dc_status DC_CALL dc_gpu_lock(dc_gpu* gpu);
DC_API dc_status DC_CALL dc_gpu_trylock(dc_gpu* gpu, int32_t* acquired);
DC_API dc_status DC_CALL dc_gpu_unlock(dc_gpu* gpu);
DC_API dc_status DC_CALL dc_gpu_lock_held(const dc_gpu* gpu, int32_t* held);

typedef struct dc_gpu_identity {
    uint32_t size;             /* sizeof(dc_gpu_identity), set by the caller */
    int32_t nvapi_ok;
    int32_t nvml_ok;
    char nvapi_error[512];     /* NvAPI.err_detail */
    char nvml_error[1024];     /* Nvml.err_detail (lists every nvml.dll location tried) */
    char nvml_path[512];
    char pairing_error[512];   /* "" when the halves agree or could not be compared */
    char requested_slot[24];
    char slot[24];             /* GPU.slot(): NVML's, else NVAPI's, else requested */
    int32_t nvapi_selected;    /* the NVAPI half selected a card (even if later disabled) */
    char nvapi_slot[24];
    uint32_t nvapi_devid;
    uint32_t nvapi_subsys;
    int32_t nvml_selected;
    char nvml_slot[24];
    int32_t nvml_has_devid;
    uint32_t nvml_devid;
    uint32_t nvml_subsys;
    int32_t nvml_index;
    /* GPU.static identity keys */
    char name[96];
    char driver[96];
    char vbios[96];
    char uuid[96];
    int32_t admin;
    uint64_t nvapi_handle;     /* NvAPI.selected["handle"] (0 when none); NvAPI.gpu is this while nvapi_ok */
} dc_gpu_identity;

DC_API dc_status DC_CALL dc_gpu_identity_get(const dc_gpu* gpu, dc_gpu_identity* identity);

/* The paired NVML device of this handle, or NULL when the NVML half is unavailable/disabled.
 * The pointer is borrowed and is valid only while this handle remains open. Takes the handle lock. */
DC_API dc_status DC_CALL dc_gpu_nvml_device(const dc_gpu* gpu, void** out);

/* The software request envelopes enforced by the core. These are not hardware measurements or
 * factory defaults. hard_floor_mv is the existing hard-de-flatten UI preset, not an enforced floor.
 * Sized so a caller with an older layout is refused before anything is copied. */
typedef struct dc_request_envelopes {
    uint32_t size;
    double volt_limit_min_mv;
    double volt_limit_max_mv;
    double volt_limit_xoc_max_mv;
    double rail_offset_max_mv;
    double rail_offset_xoc_max_mv;
    int64_t max_abs_delta_khz;
    double hard_floor_mv;
    int64_t power_limit_xoc_max_mw;
    char volt_limit_fields[4][32];
} dc_request_envelopes;
DC_API dc_status DC_CALL dc_request_envelopes_get(dc_request_envelopes* out);

/* This session's MSVDD domains, sorted: which 0 = written, 1 = seen. *count is always the required
 * number on DC_OK/DC_E_BUFFER; NULL out with capacity 0 is a count query. Nothing is copied when
 * capacity is too small. Hold dc_gpu_lock across a count/fetch pair for one consistent snapshot. */
DC_API dc_status DC_CALL dc_gpu_msvdd_domains_get(dc_gpu* gpu, uint32_t which, uint32_t* out,
                                                 size_t capacity, size_t* count);

/* One fresh nvmlDeviceGetEnforcedPowerLimit read. DC_OK with *has 0 and *mw 0 when NVML/the export
 * is absent or the call fails; a successful zero is a value. No telemetry sweep or configured/live
 * request fallback. Takes the handle lock. */
DC_API dc_status DC_CALL dc_pl_enforced_read(dc_gpu* gpu, int32_t* has, uint32_t* mw);

/* "NVAPI: ok   |   NVML: FAIL (...)   |   slot 0000:01:00.0" */
DC_API dc_status DC_CALL dc_gpu_status_line(const dc_gpu* gpu, char* buffer, size_t size, size_t* needed);

/* ---- telemetry and static capabilities (nvbackend.GPU.read / _read_static) ------------------- */

/* NVML architecture enum (1 Fermi .. 10 Blackwell), or DC_E_UNRECOGNIZED when neither library says. */
DC_API dc_status DC_CALL dc_gpu_architecture(dc_gpu* gpu, uint32_t* architecture);

/* nvbackend.GPU.read_vcore_mv: the NVVDD rail as one NVAPI VoltRailsStatus call (version 1), for
 * callers that need the rail alone (verification_p0's loaded voltage). *has 1 and *mv = value_uV /
 * 1000.0 when NVAPI is live, the interface is present, the status is 0 and the value is nonzero;
 * otherwise *has 0 and *mv 0. Takes the handle lock. DC_E_ARGUMENT for a NULL pointer (nothing is
 * written); DC_E_FAILED if a C++ exception reached the entry. */
DC_API dc_status DC_CALL dc_gpu_read_vcore_mv(dc_gpu* gpu, int32_t* has, double* mv);

typedef struct dc_fan_reading {
    int32_t has_duty;
    uint32_t duty;
    int32_t has_rpm;
    uint32_t rpm;
} dc_fan_reading;

/* nvmlFieldValue_t carried with its nvmlValueType (0 double, 1/2/3 unsigned, 4/5 signed). */
typedef struct dc_typed_value {
    int32_t present;
    uint32_t type;
    double d;
    uint64_t u;
    int64_t s;
} dc_typed_value;

/* Bits of dc_telemetry.present, in the order of the fields. Use unsigned
 * 64-bit constants: C-compatible enums cannot portably hold bits 31..34. */
#define DC_T_CORE          (UINT64_C(1) << 0)
#define DC_T_MEM           (UINT64_C(1) << 1)
#define DC_T_VIDEO         (UINT64_C(1) << 2)
#define DC_T_XBAR          (UINT64_C(1) << 3)
#define DC_T_CORE_OFF      (UINT64_C(1) << 4)
#define DC_T_MEM_OFF       (UINT64_C(1) << 5)
#define DC_T_PRIV_CLOCKS   (UINT64_C(1) << 6)
#define DC_T_TEMP_EDGE     (UINT64_C(1) << 7)
#define DC_T_TEMP_HOTSPOT  (UINT64_C(1) << 8)
#define DC_T_TEMP_DELTA    (UINT64_C(1) << 9)
#define DC_T_PL_REQUESTED  (UINT64_C(1) << 10)
#define DC_T_POWER_W       (UINT64_C(1) << 11)
#define DC_T_PL_NOW        (UINT64_C(1) << 12)
#define DC_T_PWR_GPU_PCT   (UINT64_C(1) << 13)
#define DC_T_PWR_BOARD_PCT (UINT64_C(1) << 14)
#define DC_T_PL_TARGET_PCT (UINT64_C(1) << 15)
#define DC_T_VCORE_MV      (UINT64_C(1) << 16)
#define DC_T_NUM_FANS      (UINT64_C(1) << 17)
#define DC_T_UTIL_GPU     (UINT64_C(1) << 18)
#define DC_T_UTIL_FB      (UINT64_C(1) << 19)
#define DC_T_UTIL_VID     (UINT64_C(1) << 20)
#define DC_T_UTIL_BUS     (UINT64_C(1) << 21)
#define DC_T_EVENT_MASK   (UINT64_C(1) << 22)
#define DC_T_PERF_DECREASE (UINT64_C(1) << 23)
#define DC_T_PCIE_GEN     (UINT64_C(1) << 24)
#define DC_T_PCIE_WIDTH   (UINT64_C(1) << 25)
#define DC_T_PCIE_ERR     (UINT64_C(1) << 26)
#define DC_T_PSTATE       (UINT64_C(1) << 27)
#define DC_T_VBOOST       (UINT64_C(1) << 28)
#define DC_T_CORE_P0MAX   (UINT64_C(1) << 29)
#define DC_T_MEM_P0MAX    (UINT64_C(1) << 30)
#define DC_T_ENERGY_J     (UINT64_C(1) << 31)
#define DC_T_VF_LOCKED    (UINT64_C(1) << 32)
#define DC_T_VF_LOCK_MV   (UINT64_C(1) << 33)
#define DC_T_CLK_LOCK     (UINT64_C(1) << 34)

typedef struct dc_telemetry {
    uint32_t size;          /* sizeof(dc_telemetry), set by the caller */
    uint64_t present;       /* DC_T_* bits; absent fields hold zero */
    uint32_t core, mem, video, xbar;      /* MHz */
    int32_t core_off, mem_off;            /* MHz */
    uint32_t priv_clocks[288];            /* raw private clock getter payload words */
    int32_t temp_edge;
    double temp_hotspot;
    uint32_t temp_sensor_count;
    double temp_sensors[32];
    double temp_delta;
    uint32_t pl_requested_mw;
    double power_w;
    uint32_t pl_now_mw;
    double pwr_gpu_pct, pwr_board_pct, pl_target_pct, vcore_mv;
    uint32_t num_fans;
    uint32_t fan_count;                   /* fans[] is always reported (possibly empty) */
    dc_fan_reading fans[32];
    uint32_t util_gpu, util_fb, util_vid, util_bus;
    uint64_t event_mask;
    uint32_t perf_decrease;
    uint32_t pcie_gen, pcie_width;
    dc_typed_value pcie_err[11];          /* PCIE_ERR_FIELDS order: 173..183 */
    double pcie_err_total, pcie_err_since;
    uint32_t pstate;
    int32_t vboost_pct;
    uint32_t core_p0max, mem_p0max;
    double energy_j;
    uint32_t vf_locked_count;
    uint32_t vf_locked_domains[32];
    double vf_lock_mv;
    uint32_t clk_lock_min_mhz, clk_lock_max_mhz;
    /* 1 when pl_requested_mw came from a live client-power read that succeeded inside this tick
     * (NVML's request unreadable, NVAPI live): that read back-filled the handle's absent pl_min_mw /
     * pl_def_mw / pl_max_mw capabilities (dc_gpu_capabilities_get), as Python's sweep back-fills
     * GPU.static; a caller keeping its own copy re-syncs then. 0 otherwise. */
    int32_t pl_from_live;
} dc_telemetry;

/* One telemetry tick: every reader runs; absent values are left out of present. */
DC_API dc_status DC_CALL dc_gpu_read_telemetry(dc_gpu* gpu, dc_telemetry* out);

typedef struct dc_offset_range {
    int32_t present;
    int32_t low, high, current;
} dc_offset_range;

/* Read the current offset range without using cached capabilities. Core values
 * are MHz; divide memory values by dc_gpu_mem_offset_scale().scale to obtain
 * that interface's labelled memory units (including MHz true where supported).
 * ctype is 0 for core or 2 for memory. DC_E_UNAVAILABLE means the range could
 * not be read; all failures leave out unchanged. On success present is 1. */
DC_API dc_status DC_CALL dc_gpu_clock_offset_range_get(dc_gpu* gpu, uint32_t ctype, dc_offset_range* out);

enum {
    DC_C_PL_MIN = 1u << 0, DC_C_PL_MAX = 1u << 1, DC_C_PL_DEF = 1u << 2, DC_C_GFX_MIN = 1u << 3, DC_C_GFX_MAX = 1u << 4,
    DC_C_FAN_MIN = 1u << 5, DC_C_FAN_MAX = 1u << 6, DC_C_MEM_TYPE_ID = 1u << 7, DC_C_MEM_DIV = 1u << 8,
};

typedef struct dc_capabilities {
    uint32_t size;
    uint32_t present;                     /* DC_C_* bits */
    uint32_t pl_min_mw, pl_max_mw, pl_def_mw;
    uint32_t mem_clock_count;             /* total; at most 512 are copied */
    uint32_t mem_clocks[512];
    uint32_t gfx_min, gfx_max;
    uint32_t fan_min, fan_max;
    uint32_t mem_type_id;
    char mem_type[32];
    uint32_t mem_div;
    dc_offset_range core_off_range, mem_off_range;
} dc_capabilities;

/* The non-identity keys of GPU.static. */
DC_API dc_status DC_CALL dc_gpu_capabilities_get(dc_gpu* gpu, dc_capabilities* out);


/* ---- fan control (nvbackend.GPU.read_fan_control_state .. restore_fan_control_state) --------- */

/* One fan's requested level and policy (not its RPM): what Undo restores. */
typedef struct dc_fan_row {
    uint32_t id;              /* NVML index, classic cooler index, or client cooler id */
    uint32_t level;           /* requested duty % */
    uint32_t policy;          /* NVML 0/1, classic 1/2/4/8/16, client mode 0/1 */
    uint32_t default_policy;  /* the automatic policy this fan returns to */
    int32_t manual;
    uint32_t min, max;        /* duty range the control accepts */
    int32_t has_rpm;
    uint32_t rpm;
} dc_fan_row;

typedef struct dc_fan_state {
    uint32_t size;            /* sizeof(dc_fan_state), set by the caller */
    char source[16];          /* "nvml", "nvapi_cooler" or "nvapi_client" */
    uint32_t count;
    dc_fan_row fans[32];
} dc_fan_state;

/* read_fan_control_state: DC_E_UNAVAILABLE when no interface reports requested levels. */
DC_API dc_status DC_CALL dc_gpu_fan_state_get(dc_gpu* gpu, dc_fan_state* out);

/* read_fan_manual: *manual is 1 or 0 when every fan shares that policy; DC_E_UNAVAILABLE for unknown/mixed. */
DC_API dc_status DC_CALL dc_gpu_fan_manual_get(dc_gpu* gpu, int32_t* manual);

typedef struct dc_fan_capabilities {
    uint32_t size;
    int32_t manual;           /* a manual duty can be requested */
    int32_t automatic;        /* the automatic policy can be restored */
    char source[16];          /* the interface the writers will use */
    uint32_t min, max;
} dc_fan_capabilities;

/* fan_capabilities: the shared view of UI, profiles and the writers. */
DC_API dc_status DC_CALL dc_gpu_fan_capabilities_get(dc_gpu* gpu, dc_fan_capabilities* out);

/* A writer's (ok, message) result. DC_OK means the outcome was produced; ok says whether it succeeded. */
typedef struct dc_outcome {
    int32_t ok;
    char message[4096];
} dc_outcome;

/* set_fan(percent): every fan to a manual duty through NVML, or the NVAPI coolers. */
DC_API dc_status DC_CALL dc_gpu_fan_set(dc_gpu* gpu, int32_t percent, dc_outcome* out);

/* reset_fan: every fan back to its automatic policy. */
DC_API dc_status DC_CALL dc_gpu_fan_reset(dc_gpu* gpu, dc_outcome* out);

/* A saved fan row as the caller found it. The has_* / level_valid flags carry
 * what the Python snapshot checks see, so refusals keep their exact text and order. */
typedef struct dc_fan_snapshot_row {
    int32_t has_manual;       /* "manual" is a bool */
    int32_t manual;
    int32_t has_id;           /* "id" is an integer */
    uint32_t id;
    int32_t level_valid;      /* "level" is an integer-valued number in 0..100 */
    int32_t level;
    int32_t has_policy;       /* "policy" is an integer */
    uint32_t policy;
} dc_fan_snapshot_row;

typedef struct dc_fan_snapshot {
    uint32_t size;
    int32_t is_mapping;       /* the snapshot was a mapping at all */
    char source[16];          /* "" when absent or not text */
    uint32_t count;           /* rows in the snapshot; only the first 32 are carried, and more never match a card */
    dc_fan_snapshot_row fans[32];
} dc_fan_snapshot;

/* restore_fan_control_state: requested duty and policy of every fan, through the current bindings. */
DC_API dc_status DC_CALL dc_gpu_fan_restore(dc_gpu* gpu, const dc_fan_snapshot* state, dc_outcome* out);

/* Test seam: replace the bounded wait inside readback loops. NULL restores the real sleep. */
typedef void(DC_CALL* dc_sleep_fn)(uint32_t milliseconds);
DC_API void DC_CALL dc_set_sleep_hook(dc_sleep_fn hook);
/* Test seam: replace the monotonic clock (seconds, Python's time.monotonic()) behind retry
 * cadences. NULL restores the real clock. */
typedef double(DC_CALL* dc_clock_fn)(void);
DC_API void DC_CALL dc_set_clock_hook(dc_clock_fn hook);

/* ---- voltage boost (nvbackend.GPU.read_voltage_boost / set_voltage_boost) -------------------- */

/* The VBIOS over-voltage headroom in use, 0..100 %; DC_E_UNAVAILABLE when the getter does not answer. */
DC_API dc_status DC_CALL dc_gpu_voltage_boost_get(dc_gpu* gpu, int32_t* percent);

/* Read-modify-write of the control struct; the reserved words stay as the driver wrote them. */
DC_API dc_status DC_CALL dc_gpu_voltage_boost_set(dc_gpu* gpu, int64_t percent, dc_outcome* out);

/* ---- V/F point lock and frequency lock (nvbackend.GPU.*vf_lock* / *clk_lock*) ---------------- */

typedef struct dc_vf_point_lock {
    uint32_t domain;
    uint32_t lock_mode;       /* 3: locked to the highest V/F point at or below volt_uv */
    uint32_t volt_uv;         /* the REQUESTED voltage, stored verbatim by the driver */
    uint32_t count;           /* entries in the driver's lock table */
} dc_vf_point_lock;

/* (lock, error) as read_vf_lock_status returns it: locked with the lock, an error text,
 * or neither (unlocked, and that is known). */
typedef struct dc_vf_lock_status {
    int32_t locked;
    dc_vf_point_lock lock;
    char error[256];          /* "" when the answer is authoritative */
} dc_vf_lock_status;

/* has_domain limits the check to that domain, so another tuner's lock earlier in the table cannot hide it. */
DC_API dc_status DC_CALL dc_gpu_vf_lock_status(dc_gpu* gpu, int32_t has_domain, int64_t domain, dc_vf_lock_status* out);

/* read_clk_lock: the NVML frequency lock as (min, max) MHz; DC_E_UNAVAILABLE for unlocked or unreadable. */
DC_API dc_status DC_CALL dc_gpu_clk_lock_get(dc_gpu* gpu, uint32_t* min_mhz, uint32_t* max_mhz);

typedef struct dc_clk_lock_status {
    int32_t locked;
    uint32_t min_mhz, max_mhz;
    char error[256];          /* "" when unlocked-and-known or locked */
} dc_clk_lock_status;

/* read_clk_lock_status: distinguishes unlocked from unknown ownership. */
DC_API dc_status DC_CALL dc_gpu_clk_lock_status(dc_gpu* gpu, dc_clk_lock_status* out);

/* set_vf_lock: lock the curve to the highest V/F point at or below volt_uv, on domain (or the
 * domain already locked, else 6), by read-modify-write of the driver's own buffer. */
DC_API dc_status DC_CALL dc_gpu_vf_lock_set(dc_gpu* gpu, int64_t volt_uv, int32_t has_domain, int64_t domain,
                                            dc_outcome* out);

/* vf_lock_recovery_pending: an unsuccessful hold still needs its prior state restored. */
DC_API int32_t DC_CALL dc_gpu_vf_lock_recovery_pending(const dc_gpu* gpu);

/* recover_vf_lock: retry that rollback, restoring only this request's target mode and value. */
DC_API dc_status DC_CALL dc_gpu_vf_lock_recover(dc_gpu* gpu, dc_outcome* out);

/* clear_vf_lock: lockMode 0 on every point-locked entry (or only domain; with has_expected, only if
 * that domain still holds expected_uv). Frequency-lock entries are left alone. */
DC_API dc_status DC_CALL dc_gpu_vf_lock_clear(dc_gpu* gpu, int32_t has_domain, int64_t domain, int32_t has_expected,
                                              int64_t expected_uv, dc_outcome* out);

/* vf_lock_self_test: hand the driver back the exact bytes its getter produced; a verified no-op. */
DC_API dc_status DC_CALL dc_gpu_vf_lock_self_test(dc_gpu* gpu, dc_outcome* out);

/* GPU._vf_lock_recovery: the rollback an unsuccessful hold still owes (pending 0: none). The two
 * pairs are (lockMode, volt_uV) as the record keeps them; verification is the record's
 * "verification" key, set by a verification_p0 hold (Python adds the key only when it is True). */
typedef struct dc_vf_lock_recovery {
    uint32_t size;            /* sizeof(dc_vf_lock_recovery), set by the caller */
    int32_t pending;
    int64_t domain;
    uint32_t previous_mode;
    uint32_t previous_uv;
    uint32_t requested_mode;
    uint32_t requested_uv;
    int32_t verification;
} dc_vf_lock_recovery;

/* Read the recovery record. DC_E_ARGUMENT (nothing written) for a NULL handle or out, or a size
 * that is not this header's. Takes the handle lock. */
DC_API dc_status DC_CALL dc_gpu_vf_lock_recovery_get(dc_gpu* gpu, dc_vf_lock_recovery* out);

/* Replace the recovery record (pending 0 clears it), as verification_p0 marks an existing record
 * or installs its own before recover_vf_lock. Nothing is written to the driver. DC_E_ARGUMENT
 * (nothing changed) for a NULL handle or record, or a size that is not this header's. Takes the
 * handle lock. */
DC_API dc_status DC_CALL dc_gpu_vf_lock_recovery_set(dc_gpu* gpu, const dc_vf_lock_recovery* record);

/* One domain's entry among the first count entries of the driver's own lock table (domain,
 * lockMode, volt_uV and the table's count), for verification_p0's target check.
 * DC_E_UNAVAILABLE (out untouched) when the getter does not answer or the domain is absent;
 * DC_E_ARGUMENT for a NULL handle or out. Takes the handle lock. */
DC_API dc_status DC_CALL dc_gpu_vf_lock_entry_get(dc_gpu* gpu, int64_t domain, dc_vf_point_lock* out);

/* ---- V/F curve (nvbackend.GPU.vfp_layout, read_vf_curve, read_vfp_other_rows, apply_vf_deltas,
 *      reset_vf_curve, _vf_delta_snapshot) ------------------------------------------------------- */

#define DC_VFP_POINTS 128u

/* What this card's V/F table is, probed from the driver's answers (VfpLayout). */
typedef struct dc_vf_layout {
    uint32_t size;
    uint32_t n_entries;
    uint32_t gpu_count;
    uint32_t gpu_idx[DC_VFP_POINTS];     /* the leading run of non-decreasing voltage */
    uint32_t other_count;
    uint32_t other_idx[DC_VFP_POINTS];   /* trailing rows (Pascal: the MEMORY points) */
    uint32_t freq_div;                   /* 2 when the GPU rows carry GPC2CLK */
    char notes[256];
    char error[256];                     /* _vfp_layout_error when the layout is unavailable */
} dc_vf_layout;

/* vfp_layout(force): DC_E_UNAVAILABLE with out->error when the curve APIs do not answer. */
DC_API dc_status DC_CALL dc_gpu_vf_layout(dc_gpu* gpu, int32_t force, dc_vf_layout* out);

typedef struct dc_vf_point {
    uint32_t idx;
    double volt_mv;
    double freq_mhz;                     /* evaluated, includes the current delta */
    int32_t delta_khz;
} dc_vf_point;

/* (points, err): count > 0 with points, or count 0 with the error text. */
typedef struct dc_vf_curve {
    uint32_t size;
    uint32_t count;
    dc_vf_point points[DC_VFP_POINTS];
    char error[256];
} dc_vf_curve;

DC_API dc_status DC_CALL dc_gpu_vf_curve_read(dc_gpu* gpu, dc_vf_curve* out);

typedef struct dc_vf_other_row {
    uint32_t idx;
    double volt_mv;
    double value;
    int32_t memory;                      /* its rounded value is one of the driver's mem_clocks */
} dc_vf_other_row;

typedef struct dc_vf_other_rows {
    uint32_t size;
    uint32_t count;
    dc_vf_other_row rows[DC_VFP_POINTS];
} dc_vf_other_rows;

/* read_vfp_other_rows: the rows the curve editor may not touch; count 0 for []. */
DC_API dc_status DC_CALL dc_gpu_vf_other_rows(dc_gpu* gpu, dc_vf_other_rows* out);

typedef struct dc_vf_delta {
    int64_t idx;
    int64_t delta_khz;
} dc_vf_delta;

/* apply_vf_deltas: read-modify-write of the delta table; only differing GPU rows are touched.
 * The deltas are visited in order, so the first offending row decides the refusal. */
DC_API dc_status DC_CALL dc_gpu_vf_deltas_apply(dc_gpu* gpu, const dc_vf_delta* deltas, size_t count,
                                                dc_outcome* out);

/* reset_vf_curve: every GPU point's delta to zero (the factory curve). */
DC_API dc_status DC_CALL dc_gpu_vf_curve_reset(dc_gpu* gpu, dc_outcome* out);

/* rephase_deltas: every GPU point's delta onto ONE phase of this card's grid (clock_step_khz x
 * freq_div); off-phase deltas are rounded DOWN to the most common phase and written in one
 * apply_vf_deltas. A curve-read error, a missing layout or a failed write comes back as
 * ok = 0 with the message; out->message otherwise names the points moved or that none were. */
DC_API dc_status DC_CALL dc_gpu_rephase_deltas(dc_gpu* gpu, dc_outcome* out);

/* _vf_delta_snapshot: the graphics deltas by index, or DC_E_UNAVAILABLE when the card has no readable curve. */
typedef struct dc_vf_delta_snapshot {
    uint32_t size;
    uint32_t freq_div;
    uint32_t count;
    dc_vf_delta deltas[DC_VFP_POINTS];
} dc_vf_delta_snapshot;

DC_API dc_status DC_CALL dc_gpu_vf_delta_snapshot(dc_gpu* gpu, dc_vf_delta_snapshot* out);

/* ---- clock offsets, the NVML frequency lock and the clock grid (nvbackend.GPU.set_clock_offset,
 *      clock_step_khz, lockable_clocks_by_mem, lock_gpu_clocks, reset_gpu_clocks) ---------------- */

typedef struct dc_mem_offset_scale {
    uint32_t scale;           /* NVML units per slider unit */
    char unit[16];            /* "MHz true" or "MHz eff" */
} dc_mem_offset_scale;

DC_API dc_status DC_CALL dc_gpu_mem_offset_scale(dc_gpu* gpu, dc_mem_offset_scale* out);
DC_API uint32_t DC_CALL dc_gpu_memory_offset_step_units(dc_gpu* gpu);

/* clock_step_khz / step_is_measured / max_rise_khz: this card's core-clock grid in kHz. */
DC_API dc_status DC_CALL dc_gpu_clock_step_khz(dc_gpu* gpu, uint32_t* step_khz, int32_t* measured);
DC_API dc_status DC_CALL dc_gpu_max_rise_khz(dc_gpu* gpu, uint32_t* khz);

typedef struct dc_clock_row {
    uint32_t mem_mhz;
    uint32_t count;           /* graphics clocks the driver enumerates at this memory clock */
    uint32_t graphics[512];
} dc_clock_row;

/* lockable_clocks_by_mem: one row per memory clock that answers, in mem_clocks order. rows may be
 * NULL to ask only for the count; DC_E_BUFFER when more rows exist than capacity. */
DC_API dc_status DC_CALL dc_gpu_lockable_clocks(dc_gpu* gpu, dc_clock_row* rows, size_t capacity, size_t* count);

/* set_clock_offset(ctype, mhz, preserve_curve): ctype 0 = core (snapped to the grid), 2 = memory. */
DC_API dc_status DC_CALL dc_gpu_clock_offset_set(dc_gpu* gpu, uint32_t ctype, double mhz, int32_t preserve_curve,
                                                 dc_outcome* out);

/* lock_gpu_clocks(min, max): snapped DOWN onto the lockable table before the write. */
DC_API dc_status DC_CALL dc_gpu_clocks_lock(dc_gpu* gpu, int64_t min_mhz, int64_t max_mhz, dc_outcome* out);

/* last_clock_lock_request: the last accepted snapped range; DC_E_UNAVAILABLE when none. */
DC_API dc_status DC_CALL dc_gpu_clocks_last_lock(dc_gpu* gpu, uint32_t* min_mhz, uint32_t* max_mhz);

/* reset_gpu_clocks / _reset_gpu_clocks(allow_pascal_noop). */
DC_API dc_status DC_CALL dc_gpu_clocks_reset(dc_gpu* gpu, int32_t allow_pascal_noop, dc_outcome* out);

/* ---- per-domain clock-offset control block (nvbackend.GPU.clkdom_* and the rail offsets) ------ */

#define DC_CLKDOM_SLOTS 32

/* nvbackend.ClkDomLayout: the validated layout of this card, or DC_E_UNAVAILABLE (Python None). */
typedef struct dc_clkdom_layout {
    uint32_t size;
    char name[16];
    uint32_t version;
    uint32_t block_size;
    uint32_t header;
    uint32_t stride;
    uint32_t mask_dword;
    uint32_t mode;
    uint32_t freq_khz;
    int32_t has_nvvdd_uv;
    uint32_t nvvdd_uv;
    int32_t has_msvdd_uv;
    uint32_t msvdd_uv;
} dc_clkdom_layout;

/* GPU._clkdom_capability_error: (kind, text), has_error == 0 is None. */
typedef struct dc_clkdom_error {
    uint32_t size;
    int32_t has_error;
    char kind[32];
    char text[256];
} dc_clkdom_error;

/* One entry of read_clk_domain_offsets: {mode, freq_khz (logical sign), msvdd_uv or None}. */
typedef struct dc_clkdom_request {
    uint32_t domain;
    int32_t mode;
    int32_t freq_khz;
    int32_t has_msvdd_uv;
    int32_t msvdd_uv;
} dc_clkdom_request;

typedef struct dc_clkdom_requests {
    uint32_t size;
    uint32_t count;
    dc_clkdom_request rows[DC_CLKDOM_SLOTS];
    char error[256];  /* set with DC_E_UNAVAILABLE: Python's (None, err) */
} dc_clkdom_requests;

typedef struct dc_clkdom_pair {
    uint32_t control;
    uint32_t domain;
} dc_clkdom_pair;

/* One entry of clkdom_measurements, in Python's dict order. has_value == 0 is None. */
typedef struct dc_clkdom_measurement {
    char name[16];
    int32_t has_value;
    uint32_t value_khz;
} dc_clkdom_measurement;

typedef struct dc_clkdom_probe_error {
    uint32_t domain;
    char text[64];
} dc_clkdom_probe_error;

/* rail_offset_capability's record. */
typedef struct dc_rail_offset_capability {
    uint32_t size;
    int32_t available;
    int32_t has_domain;
    int64_t domain;
    int32_t has_value;
    double value_mv;
    int32_t experimental;
    char reason[256];
} dc_rail_offset_capability;

DC_API int32_t DC_CALL dc_gpu_clkdom_ok(dc_gpu* gpu);
DC_API int32_t DC_CALL dc_gpu_clkdom_is_blackwell(dc_gpu* gpu);
/* clkdom_domains: the accepted control indices (probed once, one bit at a time). */
DC_API dc_status DC_CALL dc_gpu_clkdom_domains(dc_gpu* gpu, uint32_t* domains, size_t capacity, size_t* count);
DC_API dc_status DC_CALL dc_gpu_clkdom_layout(dc_gpu* gpu, dc_clkdom_layout* out);
DC_API dc_status DC_CALL dc_gpu_clkdom_capability_error(dc_gpu* gpu, dc_clkdom_error* out);
DC_API dc_status DC_CALL dc_gpu_clkdom_probe_errors(dc_gpu* gpu, dc_clkdom_probe_error* errors, size_t capacity,
                                                    size_t* count);
DC_API int32_t DC_CALL dc_gpu_clkdom_offset_read_pending(dc_gpu* gpu);
DC_API dc_status DC_CALL dc_gpu_clkdom_controls_for_ui(dc_gpu* gpu, uint32_t* controls, size_t capacity,
                                                       size_t* count);
DC_API dc_status DC_CALL dc_gpu_clkdom_control_polarity(dc_gpu* gpu, int64_t control, int32_t* polarity);
DC_API dc_status DC_CALL dc_gpu_clkdom_control_label(dc_gpu* gpu, int64_t control, char* buffer, size_t size,
                                                     size_t* needed);
/* clkdom_step_mhz: DC_E_UNAVAILABLE is Python None. */
DC_API dc_status DC_CALL dc_gpu_clkdom_step_mhz(dc_gpu* gpu, uint32_t* step_mhz);
/* Tri-state answers: DC_E_UNAVAILABLE is Python None, else *out is 0/1. */
DC_API dc_status DC_CALL dc_gpu_clkdom_delta_inert(dc_gpu* gpu, int32_t has_domain, int64_t domain, int32_t* out);
DC_API dc_status DC_CALL dc_gpu_clkdom_delta_clears_ceiling(dc_gpu* gpu, int64_t domain, int32_t* out);
DC_API int32_t DC_CALL dc_gpu_clkdom_offset_slider_offered(dc_gpu* gpu, int64_t control);
/* clkdom_pairing(rows): has_rows == 0 reads a snapshot; otherwise live_domains are the rows whose
 * prog_mhz is non-zero. */
DC_API dc_status DC_CALL dc_gpu_clkdom_pairing(dc_gpu* gpu, int32_t has_rows, const uint32_t* live_domains,
                                               size_t live_count, dc_clkdom_pair* pairs, size_t capacity,
                                               size_t* count);
DC_API dc_status DC_CALL dc_gpu_clkdom_measurements(dc_gpu* gpu, dc_clkdom_measurement* rows, size_t capacity,
                                                    size_t* count);
/* read_clk_domain_offsets: DC_E_UNAVAILABLE with out->error for Python's (None, err). */
DC_API dc_status DC_CALL dc_gpu_clkdom_requests(dc_gpu* gpu, dc_clkdom_requests* out);
/* read_xbar_offset_mhz: DC_E_UNAVAILABLE is Python None. */
DC_API dc_status DC_CALL dc_gpu_clkdom_xbar_offset_mhz(dc_gpu* gpu, double* mhz);
DC_API dc_status DC_CALL dc_gpu_clkdom_offset_set(dc_gpu* gpu, int64_t domain, double mhz, dc_outcome* out);
DC_API dc_status DC_CALL dc_gpu_rail_offset_capability(dc_gpu* gpu, int64_t rail, int32_t has_domain,
                                                       int64_t domain, dc_rail_offset_capability* out);
/* read_rail_offset_mv: DC_E_UNAVAILABLE is Python None. */
DC_API dc_status DC_CALL dc_gpu_rail_offset_read(dc_gpu* gpu, int64_t domain, int64_t rail, double* mv);
DC_API dc_status DC_CALL dc_gpu_rail_offset_set(dc_gpu* gpu, double mv, int64_t domain, int64_t rail,
                                                dc_outcome* out);
DC_API dc_status DC_CALL dc_gpu_msvdd_offset_reset(dc_gpu* gpu, int64_t domain, dc_outcome* out);
/* GPU.msvdd_write_enabled / voltage_xoc_enabled: the UI's opt-ins. */
DC_API dc_status DC_CALL dc_gpu_clkdom_flags_set(dc_gpu* gpu, int32_t msvdd_write_enabled,
                                                 int32_t voltage_xoc_enabled);
DC_API dc_status DC_CALL dc_gpu_clkdom_flags_get(dc_gpu* gpu, int32_t* msvdd_write_enabled,
                                                 int32_t* voltage_xoc_enabled);
/* retry_clock_capabilities(now): has_now == 0 uses a monotonic clock; *changed is the bool. */
DC_API dc_status DC_CALL dc_gpu_retry_clock_capabilities(dc_gpu* gpu, int32_t has_now, double now,
                                                         int32_t* changed);
/* refresh_capabilities: the clock-control discovery caches and the voltage-rail read caches
 * (_refresh_volt_rail_capabilities, dc_rails_refresh); previous receives the old error. */
DC_API dc_status DC_CALL dc_gpu_refresh_capabilities(dc_gpu* gpu, dc_clkdom_error* previous);

/* ---- RM escape transport, layer 0 (nvbackend.GPU._legacy_clk_gdi / _legacy_clk_escape) -------- */

/* The error model across the ABI (DESIGN-TRANSPORT.md 3.6). */
enum {
    DC_ERR_NONE = 0,
    DC_ERR_TRANSIENT = 1,   /* ValueError after touching the driver: a failed read; a retry may recover */
    DC_ERR_INVALID = 2,     /* ValueError raised before the driver was touched */
    DC_ERR_UNSUPPORTED = 3, /* CurrentLimitUnsupported: a fixed property of this image */
    DC_ERR_IDENTITY = 4,    /* PerfDiagnosticError / ClientPowerError "identity": the paired target changed */
    DC_ERR_MALFORMED = 5,   /* "malformed": a packet or transport geometry that is not understood */
    DC_ERR_UNSTABLE = 6,    /* ClientPowerError "unstable": two fresh reads disagreed */
    DC_ERR_READBACK = 7     /* ClientPowerError "readback": an exact read-back or restoration disagreed */
};
typedef struct dc_error {
    uint32_t size;           /* sizeof(dc_error), set by the caller */
    int32_t kind;            /* DC_ERR_* */
    int32_t fixed;           /* ValueError.fixed */
    int32_t has_ntstatus;    /* 0 when no escape was issued (Python prints "None") */
    int32_t ntstatus;        /* signed, as ctypes c_long reports it */
    uint32_t rm_status;      /* packet word 16 after the escape */
    char message[1024];      /* str(exc) of the Python path */
} dc_error;

typedef struct dc_escape_capability {
    uint32_t size;
    int32_t gdi32_loaded, escape, query_adapter_info, close_adapter;
    int32_t enum_adapters2, enum_adapters, open_from_display_name, open_from_hdc;
    int32_t complete;
    int32_t method;          /* 0 none, 1 enum2, 2 enum, 3 gdi display name, 4 hdc */
    uint32_t patch_bytes;    /* 14 on x64, 5 on x86: the hook's overwrite */
    char detail[256];        /* "" when complete, else what is missing */
} dc_escape_capability;
/* Never fails for a missing capability: the struct says what is missing. */
DC_API dc_status DC_CALL dc_escape_capability_get(dc_escape_capability* out);

typedef struct dc_escape_fields {
    uint32_t hAdapter, hDevice, Type, Flags, hContext;
} dc_escape_fields;

/* _legacy_clk_escape(packet, fields): packet is used in place; fields.hAdapter is replaced by the adapter
 * at the paired PCI slot. DC_OK and *ntstatus when the escape was issued; DC_E_UNAVAILABLE and *error
 * (Python's None) when it was not. Every enumerated adapter handle is closed before returning. */
DC_API dc_status DC_CALL dc_rm_escape_raw(dc_gpu* gpu, uint8_t* packet, size_t size,
                                          const dc_escape_fields* fields, int32_t* ntstatus, dc_error* error);

/* ---- RM escape transport, layers 1 and 2: the hook captures and the transport cache ----------- */
/* nvbackend.GPU._capture_current_limit_transport, _capture_legacy_clk_transport,
 * _rm_power_transport, _current_limit_transport_layouts (DESIGN-TRANSPORT.md 3.4, 3.5, 3.7). */

#include "drutacore/power_layouts.h"  /* dc_power_layout: the pure current-limit tables */
#include "drutacore/client_power.h"   /* dc_cp_*: the pure client-power tables and geometry */
#include "drutacore/power_arbitration.h"  /* the pure power-arbitration decoder */
#include "drutacore/rail_limits.h"     /* the pure rail-limit and hold-headroom helpers */

/* ---- the paired target binding (perf_diagnostics.PerfDiagnosticsReader._binding) --------------- */

typedef struct dc_target {
    uint32_t size;          /* sizeof(dc_target), set by the caller */
    char slot[24];          /* GPU.slot() */
    uint32_t architecture;  /* NVML's enum */
    uint32_t device_id;     /* NvAPI.selected devid / subsys */
    uint32_t subsystem_id;
    uint64_t handle;        /* NvAPI.gpu */
} dc_target;

/* The handle's shared binding, one sticky identity per dc_gpu (each Python reader keeps its own; the
 * consumers inside the core keep theirs). client_power nonzero applies ClientPowerController's
 * complete-identity requirement. DC_E_FAILED with *error carries the PerfDiagnosticError /
 * ClientPowerError kind (DC_ERR_IDENTITY, DC_ERR_TRANSIENT or DC_ERR_UNSUPPORTED) and message. */
DC_API dc_status DC_CALL dc_gpu_target_bind(dc_gpu* gpu, int32_t client_power, dc_target* out, dc_error* error);
/* A fresh reader: forget the sticky identity. */
DC_API dc_status DC_CALL dc_gpu_target_reset(dc_gpu* gpu);
/* NvAPI.QueryInterface presence for one id: nonzero when the binder resolves it (NvAPI._i(id) is not None). */
DC_API int32_t DC_CALL dc_nvapi_interface_present(const dc_gpu* gpu, uint32_t id);

enum { DC_TRANSPORT_POWER = 1, DC_TRANSPORT_LEGACY_CLK = 2 };

typedef struct dc_rm_transport {
    uint32_t size;            /* sizeof(dc_rm_transport), set by the caller */
    int32_t captured;         /* 0: Python's None; the other fields are then ignored */
    uint32_t header_count;    /* 17 from a capture. A fixture may store another length, which the
                                 next use rejects as Python does for len(header) != 17. */
    uint32_t header[17];      /* packet words 0..16 of the captured GET */
    dc_escape_fields fields;  /* the escape's hAdapter, hDevice, Type, Flags, hContext */
} dc_rm_transport;

/* The handle-bound readers that take or return a dc_rm_transport. */
#include "drutacore/client_power_controller.h" /* dc_cpc_*: the controller on a handle */
#include "drutacore/perf_diagnostics.h"        /* dc_perf_*: the PERF diagnostics reader */
#include "drutacore/clock_bios.h"              /* dc_clock_bios_*: the clock-BIOS reader */
#include "drutacore/legacy_p0.h"               /* dc_legacy_p0_*: the legacy P0 hold */

typedef struct dc_rm_observed {  /* _current_limit_observed_transport: {} when seen == 0 */
    uint32_t size;
    int32_t seen;
    uint32_t command_id, packet_bytes, parameter_bytes;
} dc_rm_observed;

/* The cache. get never captures; set stores unvalidated (captured = 0 clears); reset clears and, for
 * DC_TRANSPORT_POWER, also drops the layout choice, as a failed power transport does. */
DC_API dc_status DC_CALL dc_rm_transport_get(dc_gpu* gpu, int32_t kind, dc_rm_transport* out);
DC_API dc_status DC_CALL dc_rm_transport_set(dc_gpu* gpu, int32_t kind, const dc_rm_transport* transport);
DC_API dc_status DC_CALL dc_rm_transport_reset(dc_gpu* gpu, int32_t kind);
DC_API dc_status DC_CALL dc_rm_observed_get(dc_gpu* gpu, dc_rm_observed* out);
/* _current_limit_layout_choice: an index into the layouts of dc_rm_power_layouts, or none. */
DC_API dc_status DC_CALL dc_rm_layout_choice_get(dc_gpu* gpu, int32_t* has_choice, uint32_t* choice);
DC_API dc_status DC_CALL dc_rm_layout_choice_set(dc_gpu* gpu, int32_t has_choice, uint32_t choice);

/* The bare captures: one getter call under the hook; out->captured = 0 for Python's None. The power
 * capture resets the observed record first and leaves what this attempt saw. */
DC_API dc_status DC_CALL dc_rm_capture(dc_gpu* gpu, int32_t kind, dc_rm_transport* out);
/* _current_limit_profile_supported: NVAPI and a generation-policy table for this architecture. */
DC_API dc_status DC_CALL dc_rm_profile_supported(dc_gpu* gpu, int32_t* supported);
/* _rm_power_transport: the cached transport or a fresh capture, validated. DC_OK with *out, or
 * DC_E_FAILED with *error: DC_ERR_UNSUPPORTED is CurrentLimitUnsupported, DC_ERR_TRANSIENT the
 * retryable ValueError. */
DC_API dc_status DC_CALL dc_rm_power_transport(dc_gpu* gpu, dc_rm_transport* out, dc_error* error);
/* _current_limit_transport_layouts: (layout, *fallbacks) for the captured power GET as dc_power_layout
 * rows (each size set by the caller; at most capacity written, *count the total), or DC_E_FAILED. */
DC_API dc_status DC_CALL dc_rm_power_layouts(dc_gpu* gpu, dc_power_layout* out, uint32_t capacity,
                                             uint32_t* count, dc_error* error);

/* _legacy_clk_limit_records (increment 3): RM GET 0x20802077 through the legacy transport (captured on
 * a miss, dropped on a rejection). *available = 0 for Python's None; records holds 2 x 82 words. */
DC_API dc_status DC_CALL dc_legacy_clk_records(dc_gpu* gpu, uint32_t records[2][82], int32_t* available);

/* The rail splice (increment 4): _write_rail_records_locked after its Python preconditions. One
 * VoltRailsCtlGet under the hook; the recognized GET is read back and exactly one SET with the
 * prepared records is issued in its place. Never more than one SET. */
typedef struct dc_rail_write_request {
    uint32_t size;             /* sizeof(dc_rail_write_request), set by the caller */
    uint32_t control_version;  /* the profile's getter version, word 0 of the getter block */
    uint32_t rail_mask;        /* sum(1 << rail for rail in records) */
    dc_rail_record records[2];
    uint32_t record_count;
    uint32_t boost;
} dc_rail_write_request;
typedef struct dc_rail_write_result {
    uint32_t size;             /* sizeof(dc_rail_write_result), set by the caller */
    int32_t done;              /* a SET was issued */
    int32_t has_status;        /* 0: Python's None */
    uint32_t status;           /* RM status of the SET, or its NTSTATUS & 0xFFFFFFFF */
    char error[512];           /* _volt_rail_write_error; "" is None */
    int32_t has_protocol;      /* _volt_rail_write_protocol */
    uint32_t get_command, set_command, packet_size, getter_version;
    uint32_t observed_count;   /* _volt_rail_observed_packets, at most 8 */
    char observed[8][256];
} dc_rail_write_result;
DC_API dc_status DC_CALL dc_rail_write_locked(dc_gpu* gpu, const dc_rail_write_request* request,
                                              dc_rail_write_result* result);
/* Included here, not with the other headers above: rails.h's R4 outcome embeds dc_rail_write_result. */
#include "drutacore/rails.h"                   /* dc_rails_*: the voltage-rail getters */

/* ---- current-policy operations on the transport (increment 6a) ------------------------------- */
/* nvbackend.GPU._current_limit_capacity, _current_limit_abi, _current_limit_probe_info,
 * _current_limit_rm and _power_policy_table. Word buffers hold the parameter words after the
 * 17-word header; at most capacity are written and *count is the total. DC_E_FAILED carries the
 * Python exception in *error (DC_ERR_UNSUPPORTED = CurrentLimitUnsupported, DC_ERR_INVALID a
 * ValueError raised before the driver was touched, DC_ERR_TRANSIENT one raised after).
 * Every dc_cl_* entry (6a, 6b, B2 and B3) is guarded: no exception crosses the C boundary. A failure
 * inside the core (only allocation throws) returns DC_E_FAILED, with, where the entry takes a
 * dc_error, kind DC_ERR_TRANSIENT and the fixed text "native core: allocation failed";
 * dc_cl_max_words returns 0 then. */

/* The largest policy packet any layout sizes, in words: the bound for the word buffers. */
DC_API uint32_t DC_CALL dc_cl_max_words(void);
/* _current_limit_capacity(layout) */
DC_API dc_status DC_CALL dc_cl_capacity(const dc_power_layout* layout, uint32_t* capacity);
/* _current_limit_abi: the chosen layout and its index among dc_rm_power_layouts. */
DC_API dc_status DC_CALL dc_cl_abi(dc_gpu* gpu, dc_power_layout* layout, uint32_t* index, dc_error* error);
/* _current_limit_probe_info(layout): the info GET's words for one candidate layout. */
DC_API dc_status DC_CALL dc_cl_probe_info(dc_gpu* gpu, const dc_power_layout* layout, uint32_t* out, uint32_t capacity,
                                          uint32_t* count, dc_error* error);
/* _current_limit_rm(command, params, policy_mask, any_live_policy): params NULL is Python's None. */
DC_API dc_status DC_CALL dc_cl_rm(dc_gpu* gpu, uint32_t command, const uint32_t* params, uint32_t param_count,
                                  int32_t has_mask, uint32_t policy_mask, int32_t any_live_policy, uint32_t* out,
                                  uint32_t capacity, uint32_t* count, dc_error* error);
/* _power_policy_table: one validated read; the blocks are then read with dc_cl_table_get. */
DC_API dc_status DC_CALL dc_cl_policy_table(dc_gpu* gpu, dc_power_layout* layout, dc_error* error);
enum { DC_CL_TABLE_INFO = 0, DC_CL_TABLE_CONTROL = 1, DC_CL_TABLE_STATUS = 2 };
/* _last_policy_table: its layout, and each block (*present = 0 for no table or a None block). */
DC_API dc_status DC_CALL dc_cl_table_layout(dc_gpu* gpu, dc_power_layout* layout, int32_t* present);
DC_API dc_status DC_CALL dc_cl_table_get(dc_gpu* gpu, int32_t block, uint32_t* out, uint32_t capacity, uint32_t* count,
                                         int32_t* present);
DC_API dc_status DC_CALL dc_cl_table_reset(dc_gpu* gpu);
/* _power_policy_mask */
DC_API dc_status DC_CALL dc_cl_policy_mask_get(dc_gpu* gpu, uint32_t* mask);
DC_API dc_status DC_CALL dc_cl_policy_mask_set(dc_gpu* gpu, uint32_t mask);

/* ---- the policy rows, the getter's bookkeeping and the verified setter (increment 6b) ---------- */

/* The read_live_power_request branch of GPU._current_limit_state, as a callback the caller
 * supplies: nonzero when a fresh paired FE observation confirms the derived clamp (requested above
 * the policy's maximum, effective equal to it or saturated) of `policy` against the info and
 * control blocks just read. NULL never confirms. The callback runs under the GPU lock, which is
 * recursive: it may read this GPU from the same thread, as the Python binding's callback does
 * through dc_cpc_live_read (increment A6), but it must not wait on another thread that needs this
 * GPU. */
typedef int32_t (DC_CALL *dc_cl_confirm_fn)(void* context, uint32_t policy, uint32_t requested_ma,
                                            uint32_t effective_ma, const uint32_t* info, uint32_t info_count,
                                            const uint32_t* control, uint32_t control_count);

/* One row of get_current_limits / _current_limit_state. The caller sets `size`. */
typedef struct dc_current_row {
    uint32_t size;
    uint32_t policy;
    char label[64];
    uint32_t channel;
    uint32_t minimum_ma, maximum_ma, normal_maximum_ma, default_ma;
    uint32_t limit_ma, requested_ma, value_ma;
} dc_current_row;

/* One entry of GPU._current_limit_unavailable. `unsupported` is meaningful only when present
 * (Python's dict carries the key only then). The caller sets `size`. */
typedef struct dc_current_unavailable {
    uint32_t size;
    uint32_t policy;
    char label[64];
    int32_t present;
    int32_t unsupported;
    char error[256];
} dc_current_unavailable;

/* GPU._current_limit_state: the rows (at most `capacity`, *count the total), the control block it
 * read (*has_control 0 when the mask is empty: Python's None) and, through dc_cl_unavailable, the
 * entries it recorded. DC_E_FAILED with `error` is the exception Python raises; the unavailable
 * entries are reset first either way, as Python does. */
DC_API dc_status DC_CALL dc_cl_state(dc_gpu* gpu, dc_cl_confirm_fn confirm, void* context, dc_current_row* rows,
                                     uint32_t capacity, uint32_t* count, uint32_t* control, uint32_t control_capacity,
                                     uint32_t* control_count, int32_t* has_control, dc_error* error);
DC_API dc_status DC_CALL dc_cl_unavailable(dc_gpu* gpu, dc_current_unavailable* out, uint32_t capacity, uint32_t* count);

/* GPU.get_current_limits: the rows (empty on an unvalidated adapter or a failed read); the error
 * bookkeeping is read with dc_cl_error_get and dc_cl_last_rows. */
DC_API dc_status DC_CALL dc_cl_get_current_limits(dc_gpu* gpu, dc_cl_confirm_fn confirm, void* context,
                                                  dc_current_row* out, uint32_t capacity, uint32_t* count);
DC_API dc_status DC_CALL dc_cl_last_rows(dc_gpu* gpu, dc_current_row* out, uint32_t capacity, uint32_t* count);
/* which: DC_CL_ERROR (_current_limit_error) or DC_CL_READ_ERROR (_current_limit_read_error). The text
 * is NUL-terminated within `capacity`; *needed is its full length plus the terminator. */
enum { DC_CL_ERROR = 0, DC_CL_READ_ERROR = 1 };
DC_API dc_status DC_CALL dc_cl_error_get(dc_gpu* gpu, int32_t which, char* out, size_t capacity, size_t* needed);
/* GPU._current_limit_established, as a mask of policies. */
DC_API dc_status DC_CALL dc_cl_established_get(dc_gpu* gpu, uint32_t* mask);
DC_API dc_status DC_CALL dc_cl_established_set(dc_gpu* gpu, uint32_t mask);

/* GPU.set_current_limit_ma(policy, ma, pin=...): *ok and the message (NUL-terminated within
 * `capacity`, *needed its full length plus the terminator). policy and ma are the caller's ints,
 * already type-checked; a write may have happened whatever *ok says, so read the message. */
DC_API dc_status DC_CALL dc_cl_set_current_limit_ma(dc_gpu* gpu, dc_cl_confirm_fn confirm, void* context, int64_t policy,
                                                    int64_t ma, int32_t pin, int32_t* ok, char* message, size_t capacity,
                                                    size_t* needed);
/* GPU._power_policy_pins: (policy, mA) pairs in policy order; set replaces the whole mapping. */
DC_API dc_status DC_CALL dc_cl_pins_get(dc_gpu* gpu, uint32_t* policies, int64_t* values, uint32_t capacity,
                                        uint32_t* count);
DC_API dc_status DC_CALL dc_cl_pins_set(dc_gpu* gpu, const uint32_t* policies, const int64_t* values, uint32_t count);

/* ---- every policy in the table (increment B2) ---------------------------------------------------- */
/* nvbackend.GPU._policy_amount, _power_policy_rows, power_policy_unwritable_reason and
 * read_power_policies. Units: 0 mW, 1 mA, -1 Python's None (any other value prints "?"). */

/* One key of the caller's `static` dict: absent, present as None, or present with `value`. */
enum { DC_CL_STATIC_ABSENT = 0, DC_CL_STATIC_NONE = 1, DC_CL_STATIC_VALUE = 2 };
/* pl_min_mw / pl_def_mw / pl_max_mw as _power_policy_rows reads them: st.get("pl_min_mw", minimum)
 * (absent equals the row's minimum, None does not) and st.get(key) == x for the other two (absent
 * and None both unequal). The caller owns the dict, so it supplies these. The caller sets `size`. */
typedef struct dc_cl_statics {
    uint32_t size;
    int32_t pl_min_state, pl_def_state, pl_max_state; /* DC_CL_STATIC_* */
    int64_t pl_min_mw, pl_def_mw, pl_max_mw;
} dc_cl_statics;

/* One dict of _power_policy_rows. The caller sets `size`. maybe_board is "" or why this policy may
 * be the board limit; has_pinned 0 is pinned None. */
typedef struct dc_policy_row {
    uint32_t size;
    uint32_t policy, type, channel;
    int32_t unit;
    int64_t minimum, default_value, maximum, requested, limit, value;
    int32_t has_pinned;
    int64_t pinned;
    int32_t named_current, board;
    char maybe_board[96];
    int32_t consistent, in_range, writable;
} dc_policy_row;

/* _policy_amount(value, unit): "5001 W", "0.001 A", ... NUL-terminated within `capacity`, *needed its
 * full length plus the terminator. DC_E_RANGE for |value| > 2**53. */
DC_API dc_status DC_CALL dc_cl_policy_amount(int64_t value, int32_t unit, char* out, size_t capacity, size_t* needed);
/* power_policy_unwritable_reason(row), the text as dc_cl_policy_amount's. */
DC_API dc_status DC_CALL dc_cl_policy_unwritable_reason(const dc_policy_row* row, char* out, size_t capacity,
                                                        size_t* needed);
/* _power_policy_rows(layout, info, control, status, named, board_mw) on the caller's words: named is a
 * policy mask, the pins and GPU._board_policy are this handle's, and a unique board candidate is
 * stored as GPU._board_policy. At most `capacity` rows, *count the total. DC_E_RANGE: a record
 * index lies outside a block (Python's IndexError); nothing is stored. */
DC_API dc_status DC_CALL dc_cl_policy_rows(dc_gpu* gpu, const dc_power_layout* layout, const uint32_t* info,
                                           uint32_t info_count, const uint32_t* control, uint32_t control_count,
                                           const uint32_t* status, uint32_t status_count, uint32_t named,
                                           const dc_cl_statics* statics, int32_t has_board_mw, int64_t board_mw,
                                           dc_policy_row* rows, uint32_t capacity, uint32_t* count);
/* What read_power_policies reads after the table, where Python calls read_power_limit_mw: set
 * *has_board_mw / *board_mw (0: None, as when it raised), then fill *statics (its size is set), and
 * return 0. A nonzero return says the caller's reading raised what Python lets escape (the
 * caller keeps the exception): the call is then abandoned with DC_E_CALLER, before any row is
 * built, anything stored or anything written, and nothing the callback wrote is read. It runs
 * under the recursive GPU lock and only once a table is in hand. */
typedef int32_t (DC_CALL *dc_cl_policy_inputs_fn)(void* context, int32_t* has_board_mw, int64_t* board_mw,
                                                  dc_cl_statics* statics);
/* GPU.read_power_policies: DC_OK with the rows, or DC_E_FAILED with Python's error text in
 * error->message (kind DC_ERR_INVALID; DC_ERR_TRANSIENT is the guard's allocation failure) and no
 * rows, or DC_E_CALLER when inputs returned nonzero (no rows, nothing stored). confirm is
 * get_current_limits' callback; inputs must not be NULL. A unique board candidate is stored as
 * GPU._board_policy. */
DC_API dc_status DC_CALL dc_cl_power_policies_read(dc_gpu* gpu, dc_cl_confirm_fn confirm, void* context,
                                                   dc_cl_policy_inputs_fn inputs, void* inputs_context,
                                                   dc_policy_row* rows, uint32_t capacity, uint32_t* count,
                                                   dc_error* error);

/* ---- writing one policy and keeping the user's values (increment B3) ------------------------------ */
/* nvbackend.GPU._write_policy_limit, set_power_policy (with _pin) and _reapply_power_policy_pins.
 * Each returns DC_OK with *ok and the message NUL-terminated within `capacity` (*needed its full
 * length plus the terminator), as dc_cl_set_current_limit_ma. A message can exceed any fixed
 * buffer (a FAILED note joins one _write_policy_limit text per re-applied policy, each carrying
 * driver error texts), and these calls write: never repeat one to fetch a longer text, read it
 * with dc_cl_policy_message_get, which keeps the last of these messages in full. DC_E_RANGE is the
 * IndexError Python lets escape (a record index outside a block; nothing written to *ok or the
 * buffer). The pins are this handle's process state, never persisted: a driver restart forgets
 * them. */
/* _write_policy_limit(policy, value): the table is read, this policy's limit word changed, the SET
 * selects only it, the table is read back; any failure replays the original record and reports
 * whether its limit word read back. value is packed into the u32 limit word as ctypes does. */
DC_API dc_status DC_CALL dc_cl_write_policy_limit(dc_gpu* gpu, int64_t policy, int64_t value, int32_t* ok,
                                                  char* message, size_t capacity, size_t* needed);
/* set_power_policy(policy, value, pin=, allow_named=): rows from GPU.read_power_policies (confirm
 * and inputs as dc_cl_power_policies_read's; inputs must not be NULL; a unique board candidate is
 * stored as GPU._board_policy; inputs returning nonzero is DC_E_CALLER: nothing written, pinned or
 * stored, *ok, the buffer and dc_cl_policy_message_get's text untouched), then the refusals in Python's order, a named current policy through
 * dc_cl_set_current_limit_ma's setter (with pin) when allow_named, else dc_cl_write_policy_limit and,
 * on success with pin, the pin. whole is Python's `type(value) is int` (value then the int, clamped
 * to int64: beyond it lies beyond every range); with whole 0 value is not read. */
DC_API dc_status DC_CALL dc_cl_set_power_policy(dc_gpu* gpu, dc_cl_confirm_fn confirm, void* context,
                                                dc_cl_policy_inputs_fn inputs, void* inputs_context, int64_t policy,
                                                int32_t whole, int64_t value, int32_t pin, int32_t allow_named,
                                                int32_t* ok, char* message, size_t capacity, size_t* needed);
/* _reapply_power_policy_pins(board_mw): has_board_mw 0 is None. A unique mW policy whose request is
 * board_mw is stored as GPU._board_policy and its pin dropped; every other pin the table disagrees
 * with is written again (in policy order). *ok 1 with "" when nothing moved. */
DC_API dc_status DC_CALL dc_cl_reapply_pins(dc_gpu* gpu, int32_t has_board_mw, int64_t board_mw, int32_t* ok,
                                            char* message, size_t capacity, size_t* needed);
/* The last message of the three calls above, in full, as dc_cl_error_get's text. The message is
 * kept per handle, not per thread: a caller that writes from several threads serializes each
 * call with its fetch by holding dc_gpu_lock across both (the Python binding's `with self._lock`
 * is that mutex). */
DC_API dc_status DC_CALL dc_cl_policy_message_get(dc_gpu* gpu, char* out, size_t capacity, size_t* needed);

/* Test seams. While a resolver is installed the hook uses a buffer patcher (a served D3DKMTEscape is a
 * trampoline that must never be overwritten) and the thunk body is reached through dc_hook_test_entry,
 * which returns the forwarded NTSTATUS. */
DC_API int32_t DC_CALL dc_hook_test_entry(void* escape);
DC_API dc_status DC_CALL dc_hook_test_state(int32_t* installed, int32_t* patched, uint32_t* calls);
DC_API void DC_CALL dc_hook_test_fail_protect(int32_t fail);
/* The thunk bytes for a pointer width (pure): 14 bytes for 8, 5 for 4; DC_E_ARGUMENT otherwise. */
DC_API dc_status DC_CALL dc_hook_thunk(uint64_t address, uint64_t target, uint32_t pointer_bytes,
                                       uint8_t out[14], uint32_t* size);

#ifdef __cplusplus
}
#endif

#include "drutacore/i2c.h"            /* dc_i2c_read/write: selected GPU transport */
#include "drutacore/clkdom.h"         /* private clock-domain rows and names */
#include "drutacore/power_limit.h"    /* dc_pl_*: the board power limit (needs dc_cl_statics) */
#include "drutacore/vfplan.h"  /* the pure V/F planners */
#include "drutacore/reset.h"      /* dc_gpu_reset_all (needs dc_cl_statics, dc_rails_setter_writes) */
#endif /* DRUTACORE_H */
