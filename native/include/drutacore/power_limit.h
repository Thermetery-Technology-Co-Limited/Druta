/* Druta - a monitor and tuner for NVIDIA GPUs.
 * Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The board power limit (increment A8): nvbackend.GPU.POWER_LIMIT_XOC_MAX_MW,
 * _normal_power_bounds_mw, _power_request_bounds_mw, power_limit_bounds_mw, _read_power_limit_nvml,
 * read_power_limit_mw, set_power_limit_mw and _set_power_limit_nvml: dc_pl_*. Included by
 * drutacore.h; do not include directly.
 *
 * The configured request is NVML's nvmlDeviceGetPowerManagementLimit (status 0 and a nonzero value),
 * never the enforced limit. When it is unreadable and NVAPI is live, the live client-power request
 * (dc_cpc_live_read's read with its bookkeeping) stands in. A request outside the normal range, a
 * return from outside it, a missing NVML setter, or an unreadable NVML request goes to the live
 * client-power SET (dc_cpc_set_request's, with its single compensation); otherwise NVML's
 * nvmlDeviceSetPowerManagementLimit writes it and the request is read back exactly. After an NVML
 * write, and after a live SET whose definition is a v30 role (ROLE_INFO byte 4 == 0x30), the user's
 * policy pins are re-applied (dc_cl_reapply_pins'). No driver version, board, device, subsystem or
 * VBIOS identity is consulted. Everything is session-only: nothing is stored for, or re-applied
 * after, a GPU driver restart.
 *
 * The caller owns GPU.static: `statics` carries its pl_min_mw / pl_def_mw / pl_max_mw as read at the
 * call (dc_cl_statics, DC_CL_STATIC_*; absent and None are alike here). A successful live read inside
 * a call back-fills the handle's capability store (dc_gpu_capabilities_get) where a key is absent
 * there, as dc_cpc_live_read does; a key the caller passed absent or None then takes the store's
 * value for the rest of the call, and the entries taking a non-const statics write it back
 * (DC_CL_STATIC_VALUE with that value) so the caller can update its dict at the point Python's
 * read_live_power_request does. A key the caller passed with a value is never changed. The normal
 * range falls back to 50000 / 400000 mW for a missing key: Python's values, kept for parity, not
 * device facts.
 *
 * Every entry taking a dc_gpu holds the handle lock for the whole call (recursive: a callback of
 * another entry may call these). Returns: DC_OK; DC_E_ARGUMENT for a NULL handle or pointer or a
 * statics / error whose size is not set (nothing is read or written); DC_E_FAILED when a C++
 * exception reached the entry (only allocation can throw; the dc_error, where one is passed, says
 * so as DC_ERR_TRANSIENT). */
#ifndef DRUTACORE_POWER_LIMIT_H
#define DRUTACORE_POWER_LIMIT_H

#ifdef __cplusplus
extern "C" {
#endif

/* GPU.POWER_LIMIT_XOC_MAX_MW: the software request envelope, not a physical ceiling. */
#define DC_PL_XOC_MAX_MW 1999000

/* _normal_power_bounds_mw: (pl_min_mw or 50000, pl_max_mw or 400000). Pure. */
DC_API dc_status DC_CALL dc_pl_normal_bounds(const dc_cl_statics* statics, int64_t* low, int64_t* high);
/* _power_request_bounds_mw(current, xoc=, live=): has_current 0 is a current that is not an int
 * (None, a float, a bool: Python's `type(current) is int` is the caller's); a current outside
 * 0 < current < 0xFFFFFFFF does not widen the range. Pure. */
DC_API dc_status DC_CALL dc_pl_request_bounds(const dc_cl_statics* statics, int32_t has_current, int64_t current,
                                              int32_t xoc, int32_t live, int64_t* low, int64_t* high);
/* power_limit_bounds_mw(xoc=): xoc is the caller's flag (Python defaults it to voltage_xoc_enabled).
 * May run a live read (its bookkeeping and back-fill included); a transient live failure with a last
 * observation still counts as live. */
DC_API dc_status DC_CALL dc_pl_bounds(dc_gpu* gpu, dc_cl_statics* statics, int32_t xoc, int64_t* low,
                                      int64_t* high, dc_error* error);
/* _read_power_limit_nvml: *has 0 is None. No live read. */
DC_API dc_status DC_CALL dc_pl_read_nvml(dc_gpu* gpu, int32_t* has, uint32_t* mw);
/* read_power_limit_mw: NVML, else (NVAPI live) the live request; *has 0 is None. */
DC_API dc_status DC_CALL dc_pl_read(dc_gpu* gpu, dc_cl_statics* statics, int32_t* has, uint32_t* mw,
                                    dc_error* error);
/* set_power_limit_mw(mw): *ok and the message NUL-terminated within `capacity` (*needed its full
 * length plus the terminator), as dc_cl_reapply_pins. mw is the caller's int, already refused unless
 * it is a whole finite number (Python's type gate); outside 0 < mw < 2**32 the core refuses it with
 * Python's text before any read. xoc is bool(voltage_xoc_enabled). The message can exceed any fixed
 * buffer (it carries the re-application note, which joins one policy-writer text per pin, and the
 * live SET's error with its rollback detail), and this call writes: never repeat it to fetch a longer
 * text; read it with dc_pl_message_get. DC_E_RANGE is the IndexError Python lets escape from the pin
 * re-application (nothing written to *ok, the buffer or the kept message). */
DC_API dc_status DC_CALL dc_pl_set(dc_gpu* gpu, dc_cl_statics* statics, int64_t mw, int32_t xoc, int32_t* ok,
                                   char* message, size_t capacity, size_t* needed);
/* The last dc_pl_set, dc_pl_stock_power_policy or dc_pl_native_power_limit text in full (including
 * the dc_pl_set step inside dc_pl_max_all; a successful native read keeps ""). Kept per handle, not
 * per thread: a caller that sets from several threads serializes each call with its fetch (the
 * Python binding holds its own lock). */
DC_API dc_status DC_CALL dc_pl_message_get(dc_gpu* gpu, char* out, size_t capacity, size_t* needed);
/* _set_power_limit_nvml(mw): the NVML write of u32(mw) (ctypes' packing: mw modulo 2**32), then
 * read_power_limit_mw (live fallback included) compared with mw. *has_got 0 is got None. The message
 * is "power limit failed: " + NVML's error string (truncated to dc_outcome's buffer, never reached
 * by NVML's own texts) or one of Python's fixed texts. DC_E_UNAVAILABLE: NVML is not live or has no
 * setter export (Python's attribute access raises); nothing is written. */
DC_API dc_status DC_CALL dc_pl_set_nvml(dc_gpu* gpu, dc_cl_statics* statics, int64_t mw, dc_outcome* out,
                                        int32_t* has_got, uint32_t* got);

/* ---- increment B4: nvbackend.GPU.stock_power_policy, max_all_power_policies and
 * read_native_power_limit_mw. They run wholly in the core under the handle lock and call no caller
 * code: the derived-clamp confirmation of _current_limit_state and read_power_policies' inputs
 * (read_power_limit_mw, the static keys as that read leaves them, GPU._board_policy) are answered
 * with the same live read the dc_cl_* callbacks of the Python binding make. A read_power_policies
 * board candidate is stored as dc_cl_power_policies_read stores it. The pins these drop or clear are
 * process state (dc_cl_pins_*): nothing is persisted, and nothing is re-applied after a GPU driver
 * restart. `statics` is written back as dc_pl_set's, in every outcome. */

/* stock_power_policy(policy): *ok and the message as dc_pl_set's (sized text; a longer text is read
 * with dc_pl_message_get, never by repeating this call, which writes). The policy's pin is dropped
 * first, before any read or refusal. policy is the int64 that Python's lookups match (the binding
 * maps a policy that is not a plain int to the index it equals, or -1); whole is
 * `type(policy) is int` (0: the default write is refused as _write_policy_limit refuses it, after its
 * table read). name (name_size bytes, not NUL-terminated) is str(policy) for the messages.
 * DC_E_RANGE: the IndexError Python lets escape (read_power_policies, _write_policy_limit, the pin
 * re-application); DC_E_UNAVAILABLE: the AttributeError of _set_power_limit_nvml when the board limit
 * must be re-sent and NVML is not live or has no setter export (the default is written by then).
 * After either, *ok, the buffer and the kept message are unchanged. */
DC_API dc_status DC_CALL dc_pl_stock_power_policy(dc_gpu* gpu, dc_cl_statics* statics, int64_t policy,
                                                  int32_t whole, const char* name, size_t name_size, int32_t* ok,
                                                  char* message, size_t capacity, size_t* needed);

/* At most one step for the board limit and one per policy of a 32-bit mask. */
#define DC_PL_MAX_STEPS 33
#define DC_PL_STEP_LABEL 0
#define DC_PL_STEP_MESSAGE 1

/* One (step, ok, message) of max_all_power_policies. The caller sets size; label and message are
 * NUL-terminated and truncated to their buffers, *_needed being each full length plus the
 * terminator (read a longer text with dc_pl_step_text_get). */
typedef struct dc_policy_step {
    uint32_t size; /* sizeof(dc_policy_step) */
    int32_t ok;
    uint32_t label_needed;
    uint32_t message_needed;
    char label[128];
    char message[1024];
} dc_policy_step;

/* max_all_power_policies: every pin cleared; the board limit raised to pl_max_mw (with xoc, to the
 * live request envelope of dc_pl_bounds when that is higher) through dc_pl_set's path; then every
 * named current policy below the ceiling its own slider allows (the maximum with xoc, else the larger
 * of its normal maximum and its effective limit, never above its maximum) through
 * dc_cl_set_current_limit_ma's path unpinned, and every other writable policy below its maximum to
 * that maximum. xoc is bool(voltage_xoc_enabled); the caller passes pl_max_mw only as a value it can
 * carry exactly. *count is the number of steps (at most DC_PL_MAX_STEPS); the first min(*count,
 * capacity) are written, each entry of `steps` up to capacity needing its size set. The steps are
 * kept per handle (dc_pl_step_text_get). DC_E_RANGE: the IndexError Python lets escape (*count 0,
 * no steps kept; the writes made before it stand, as in Python). */
DC_API dc_status DC_CALL dc_pl_max_all(dc_gpu* gpu, dc_cl_statics* statics, int32_t xoc, dc_policy_step* steps,
                                       uint32_t capacity, uint32_t* count);
/* The kept text of step `index` of the last dc_pl_max_all: field DC_PL_STEP_LABEL or
 * DC_PL_STEP_MESSAGE, sized text as dc_pl_message_get's. DC_E_RANGE: no such step. */
DC_API dc_status DC_CALL dc_pl_step_text_get(dc_gpu* gpu, uint32_t index, int32_t field, char* out,
                                             size_t capacity, size_t* needed);

/* read_native_power_limit_mw: *has_value 1 and *value the bound milliwatt policy's effective STATUS
 * word, read in a fresh policy table whose INFO and CONTROL bytes equal a fresh live observation's
 * (dc_cpc_live_read's, with its bookkeeping and back-fill); else *has_value 0 and the error as sized
 * text (also kept: dc_pl_message_get). Python's IndexError inside its try is its text,
 * "list index out of range". Read-only towards the driver. */
DC_API dc_status DC_CALL dc_pl_native_power_limit(dc_gpu* gpu, dc_cl_statics* statics, int32_t* has_value,
                                                  uint32_t* value, char* error, size_t capacity, size_t* needed);

#ifdef __cplusplus
}
#endif

#endif /* DRUTACORE_POWER_LIMIT_H */
