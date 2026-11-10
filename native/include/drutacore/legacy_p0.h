/* Druta - a monitor and tuner for NVIDIA GPUs.
 * Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The legacy P0 hold (nvbackend.GPU.legacy_p0_supported, legacy_p0_owned, hold_legacy_p0,
 * release_legacy_p0 and the request half of verification_legacy_p0): dc_legacy_p0_*.
 * Included by drutacore.h; do not include directly. Increment G2.
 *
 * ForcePstate (NVAPI 0x025BFB10) with (0, 2) requests P0 and with (16, 2) returns the card to
 * automatic performance states. Support is decided at run time: NVAPI live, the interface present,
 * no NVAPI/NVML pairing error, and an NVML (or NVAPI) architecture of Kepler (2) or Maxwell (3); an
 * unreadable architecture is unsupported. No board, VBIOS or driver identity is consulted. Ownership
 * is this session's own request (GPU._legacy_p0_owned), kept on the handle for its lifetime and never
 * persisted.
 *
 * Messages are the Python texts:
 *   "legacy P0 hold is unavailable on this GPU/driver"
 *   "P0 request failed (NVAPI status N)"
 *   "P0 held; core C MHz, memory M MHz (not a maximum-core lock)"  (C is "?" when no core clock)
 *   "P0 and its top memory band were not observed" + "; existing session P0 hold retained"
 *       or + "; " + the release message, or + "; RELEASE FAILED: " + the release message
 *   "this session owns no legacy P0 hold"
 *   "P0 release failed (NVAPI status N)"
 *   "P0 request released; automatic performance states restored"
 *   "Kepler P0 request is unavailable; verification cannot write"
 *   "Kepler P0 request failed (NVAPI status N)"
 * Python's exception texts ("P0 request failed: {exc}", "P0 verification failed: {exc}", "P0
 * release failed: {exc}") have no native counterpart: telemetry reads do not throw and a faulting
 * export is not guarded.
 *
 * Returns: DC_OK when the answer or outcome was produced; DC_E_ARGUMENT for a NULL handle or out
 * pointer (nothing is written); DC_E_FAILED if a C++ exception reached the entry (only allocation
 * can throw). A driver status never fails an entry: it is in the outcome. */
#ifndef DRUTACORE_LEGACY_P0_H
#define DRUTACORE_LEGACY_P0_H

#ifdef __cplusplus
extern "C" {
#endif

/* legacy_p0_supported: *out 1 or 0. Takes the handle lock. */
DC_API dc_status DC_CALL dc_legacy_p0_supported(dc_gpu* gpu, int32_t* out);

/* legacy_p0_owned: *out 1 when this session owns a P0 request. Takes the handle lock. */
DC_API dc_status DC_CALL dc_legacy_p0_owned(dc_gpu* gpu, int32_t* out);

/* hold_legacy_p0: request P0 unless already owned (owning it before the call; a nonzero status
 * clears ownership), then up to 20 telemetry samples 100 ms apart (the dc_set_sleep_hook wait after
 * every sample that does not complete the hold) until three consecutive samples show pstate 0, a
 * nonzero P0 memory maximum and a memory clock of at least 97 % of it. On failure an already-owned
 * hold is retained; a hold this call requested is released.
 * Locking: the handle lock is taken around the request, around each sample and around the release,
 * and the entry gives its level back across the waits, so a concurrent telemetry read is not
 * blocked for the up to two seconds the verification takes. A caller that holds a level of its own
 * (dc_gpu_lock) keeps the mutex across the waits too: that is allowed, and it is what Python does
 * (nvbackend's hold_legacy_p0 holds GPU._lock for the whole call; the difference is recorded in
 * native/README.md). */
DC_API dc_status DC_CALL dc_legacy_p0_hold(dc_gpu* gpu, dc_outcome* out);

/* release_legacy_p0: ForcePstate(16, 2) when owned; ownership is cleared only on status 0. Takes the
 * handle lock for the whole call (verification_legacy_p0 holds GPU._lock around its release). */
DC_API dc_status DC_CALL dc_legacy_p0_release(dc_gpu* gpu, dc_outcome* out);

/* The request half of verification_legacy_p0, under the handle lock for the whole call (Python holds
 * GPU._lock around it). Not Kepler or not supported: not ok, "Kepler P0 request is unavailable;
 * verification cannot write", *acquired 0, no call. Already owned: ok, empty message, *acquired 0,
 * no call. Otherwise ownership is set, *acquired 1, then ForcePstate(0, 2); a nonzero status is not
 * ok ("Kepler P0 request failed (NVAPI status N)") and ownership stays set, so the caller's release
 * (dc_legacy_p0_release) still runs, as Python's finally does. *acquired is written even when the
 * entry returns DC_E_FAILED (an exception after ownership was taken): a caller reads it whatever the
 * status and releases when it is 1. */
DC_API dc_status DC_CALL dc_legacy_p0_request(dc_gpu* gpu, int32_t* acquired, dc_outcome* out);

#ifdef __cplusplus
}
#endif

#endif /* DRUTACORE_LEGACY_P0_H */
