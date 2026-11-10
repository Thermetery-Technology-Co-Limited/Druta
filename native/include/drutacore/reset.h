/* Druta - a monitor and tuner for NVIDIA GPUs.
 * Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Reset all (increment G3): nvbackend.GPU.reset_all as one entry that returns named steps:
 * dc_gpu_reset_all and dc_gpu_reset_message_get. Included by drutacore.h (it needs dc_cl_statics,
 * dc_cl_confirm_fn, dc_cl_policy_inputs_fn and dc_rails_setter_writes); do not include directly.
 *
 * The core composes the knobs that are already native, under the handle lock, in Python's order:
 *   the session's policy pins dropped first; "core offset", "mem offset" (always, preserve_curve
 *   off); where the clock-control layout validates: "clock-domain offsets" (one failure) or
 *   "<name> offset" per domain carrying a frequency offset (CLKDOM_NAMES, else "domain N"), then
 *   "core rail offset" (failure when unreadable, a write when nonzero, nothing when zero) when the
 *   layout has the NVVDD field; "MSVDD request N" for every domain this session wrote or saw plus
 *   the readable candidate (failure when unreadable, a reset when nonzero); "rail limits" (failure
 *   when the profile is known and the read is missing or lacks a first-read rail, else the reset of
 *   this session's first-read values when any field differs by more than 1e-6 mV); "current
 *   limits" (failure when the profile is supported and the read failed) and the lower-cased label
 *   of each current row off its default (set_current_limit_ma(policy, default, pin=False)); "power
 *   limit" (set_power_limit_mw(pl_def_mw) when the caller's pl_def_mw is truthy, else the failure
 *   "power limit: default unknown, left unchanged"); "power policies" (failure only when the
 *   current-limit read error is set) or "power policy N" for each writable row whose request is off
 *   its in-range default; "clock lock" (the NVML lock release, the Pascal not-supported no-op
 *   allowed); "legacy P0 hold" when this session owns one; "v/f point lock" when the lock pair is
 *   present; "fan" (always); "voltage boost" when NVAPI is live and both voltage-control
 *   interfaces are present; "vf curve" when the curve applies (not Kepler) and the boost-table
 *   setter is present. The interface checks are capability probes, never a driver-version, device,
 *   subsystem or VBIOS decision. Everything is session-only: nothing is persisted or re-applied
 *   after a GPU driver restart.
 *
 * The names "clock lock", "v/f point lock" and "legacy P0 hold" are a contract with the front ends
 * (the lock banners come down only when the step of that name succeeded).
 */
#ifndef DRUTACORE_RESET_H
#define DRUTACORE_RESET_H

#ifdef __cplusplus
extern "C" {
#endif

/* The most steps one dc_gpu_reset_all can produce: 2 offsets + 32 clock domains + core rail offset
 * + 32 MSVDD domains + rail limits + (current-limit failure + 32 current rows) + power limit + 32
 * power policies + clock lock, P0 hold, V/F point lock, fan, voltage boost, vf curve. */
#define DC_RESET_STEPS_MAX 140u
#define DC_RESET_NAME_MAX 64u
#define DC_RESET_MESSAGE_MAX 4096u

/* One step: Python's ResetStep(name, (ok, message)). message holds the text NUL-terminated within
 * DC_RESET_MESSAGE_MAX bytes; message_length is its full length in bytes (without the terminator).
 * A longer text (set_power_limit_mw's pin note or rollback detail, a policy write's restore note)
 * is truncated here and kept in full on the handle: fetch it with dc_gpu_reset_message_get. The
 * caller sets `size` on each element. */
typedef struct dc_reset_step {
    uint32_t size;
    char name[DC_RESET_NAME_MAX];
    int32_t ok;
    uint32_t message_length;
    char message[DC_RESET_MESSAGE_MAX];
} dc_reset_step;

/* The caller's GPU.static pl_* keys for set_power_limit_mw. Called with before == NULL where Python
 * reads self.static["pl_def_mw"]: fill *statics (its size is set) with the keys as they stand now
 * and return 0. Called again after set_power_limit_mw with before (what it was given) and statics
 * (what the live read left): apply the back-fill where Python's read_live_power_request changes the
 * dict, and return 0. A nonzero return says reading or updating the dict raised what Python lets
 * escape (the caller keeps the exception): the call stops there with DC_E_CALLER. For the first read
 * that is where Python stops. For the back-fill it is not: Python back-fills self.static inside
 * read_live_power_request, before set_power_limit_mw writes anything, while the core runs
 * set_power_limit_mw to completion (its NVML or live SET included) and only then calls the back-fill,
 * so a back-fill that raises leaves the power limit written where Python's leaves it unwritten. Only
 * a static mapping whose item assignment raises can tell the two apart. It runs under the
 * recursive handle lock and must not wait for any lock the caller's other threads hold while
 * calling into the core. */
typedef int32_t (DC_CALL *dc_reset_statics_fn)(void* context, const dc_cl_statics* before,
                                              dc_cl_statics* statics);

/* nvbackend.GPU.reset_all. confirm (may be NULL: never confirms) is the derived-clamp confirmation
 * of get_current_limits / set_current_limit_ma / read_power_policies, as dc_cl_get_current_limits';
 * inputs is read_power_policies' (dc_cl_power_policies_read's); statics is above; xoc is
 * bool(voltage_xoc_enabled) for set_power_limit_mw.
 *
 * steps / capacity: capacity must be at least DC_RESET_STEPS_MAX, checked before anything is done:
 * otherwise DC_E_BUFFER with *count = DC_RESET_STEPS_MAX and nothing read, written or changed (the
 * pins included). rail_writes (may be NULL; its size set by the caller) receives what the
 * rail-limit reset's _write_rail_records calls left in the writer's attributes, as
 * dc_rails_reset_limits' `writes`, whatever the status.
 *
 * Returns DC_OK with *count steps in order (every message also kept on the handle); DC_E_CALLER when
 * a callback (statics or inputs) reported its exception; DC_E_RANGE for the IndexError Python lets
 * escape set_power_limit_mw, read_power_policies or _write_policy_limit; DC_E_FAILED (error, when
 * given, kind DC_ERR_TRANSIENT with the core's fixed text) when a C++ exception reached the entry
 * (only allocation can throw); DC_E_ARGUMENT for a NULL handle, steps, count, inputs or statics, or
 * a size mismatch. On every status but DC_OK and DC_E_BUFFER the steps so far are lost, as Python's
 * raise loses them, while everything already written stays written and *count is 0. */
DC_API dc_status DC_CALL dc_gpu_reset_all(dc_gpu* gpu, dc_cl_confirm_fn confirm, void* confirm_context,
                                          dc_cl_policy_inputs_fn inputs, void* inputs_context,
                                          dc_reset_statics_fn statics, void* statics_context, int32_t xoc,
                                          dc_reset_step* steps, uint32_t capacity, uint32_t* count,
                                          dc_rails_setter_writes* rail_writes, dc_error* error);

/* The full message of step `index` of this handle's last successful dc_gpu_reset_all, NUL-terminated
 * within `capacity`; *needed its full length plus the terminator. DC_E_RANGE for an index past the
 * last call's steps. */
DC_API dc_status DC_CALL dc_gpu_reset_message_get(dc_gpu* gpu, uint32_t index, char* out, size_t capacity,
                                                  size_t* needed);

#ifdef __cplusplus
}
#endif

#endif /* DRUTACORE_RESET_H */
