// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The legacy P0 hold (port of nvbackend.GPU.legacy_p0_supported, legacy_p0_owned, hold_legacy_p0 and
// release_legacy_p0, with the request half of verification_legacy_p0): increment G2.
//
// One NVAPI call, ForcePstate (0x025BFB10, (handle, pstate index, fallback flag)): (0, 2) asks for P0,
// (16, 2) returns the card to automatic performance states. Support is decided at run time from the
// API (NVAPI live, the interface present, the NVAPI/NVML pairing intact) and the architecture
// generation (Kepler or Maxwell); no device, subsystem, VBIOS or driver identity is consulted, and
// nvbackend's LEGACY_P0_PROFILES table of measured boards is deliberately not ported. A hold is
// accepted only once telemetry shows P0 with the top memory band for three consecutive samples.
//
// Ownership is this session's own request, not a reading of the driver: the API cannot report
// another process's force request. It lives on the handle (GPU._legacy_p0_owned) and is never
// persisted or reapplied; the hold is session-only state.
//
// Not reachable natively: Python's two `except Exception` branches of hold_legacy_p0 (a ctypes
// OSError raised from ForcePstate, "P0 request failed: {exc}", and an exception from read(),
// "P0 verification failed: {exc}") and the one of release_legacy_p0 ("P0 release failed: {exc}").
// Telemetry::read() does not throw, and the core, like every other NVAPI call it makes, does not
// guard an export that faults, so a faulting ForcePstate is not survived.
//
// Locking: supported(), owned(), release() and request() expect the caller to hold Gpu::lock() (the
// C ABI entries take it). hold() takes Gpu::lock() itself around each step that touches the driver
// or the ownership flag (the request, each telemetry sample, the release) and releases it across
// each 100 ms wait, so a concurrent telemetry tick is not blocked for the two seconds the
// verification may take. A caller that holds the lock around hold() (dc_gpu_lock, or the binding's
// `with gpu._lock`) keeps it across the waits: the lock is recursive and hold() gives back only its
// own level. That is allowed, and it is how a caller gets Python's behaviour (native/README.md).
#pragma once

#include <cstdint>

#include "backend/gpu.h"
#include "backend/outcome.h"
#include "backend/telemetry.h"

namespace druta::backend {

class LegacyP0 {
public:
    LegacyP0(Gpu& gpu, Telemetry& telemetry) : gpu_(gpu), telemetry_(telemetry) {}

    // legacy_p0_supported: Kepler and Maxwell probe P0 at run time, independent of board identity.
    bool supported();
    // legacy_p0_owned: this session's ownership, not a claim to read another tuner's force state.
    bool owned() const { return owned_; }
    // hold_legacy_p0: request P0 (unless already owned) and confirm it in telemetry.
    Outcome hold();
    // release_legacy_p0: return an owned request to automatic performance states.
    Outcome release();
    // The request half of verification_legacy_p0. Not Kepler, or not supported(): no call, not ok,
    // "Kepler P0 request is unavailable; verification cannot write" (Python's refusal, which its
    // caller raises before the request). When not owned: owned = true, then ForcePstate(gpu, 0, 2);
    // acquired reports that this call took ownership. A nonzero status leaves ownership set, as Python leaves _legacy_p0_owned set
    // and raises, so the caller's release still runs. Already owned: no call, acquired false, ok
    // with an empty message.
    Outcome request(bool& acquired);

private:
    int force(std::uint32_t pstate, std::uint32_t flags);  // ForcePstate(gpu.nvapi.gpu, pstate, flags)

    Gpu& gpu_;
    Telemetry& telemetry_;
    bool owned_ = false;  // GPU._legacy_p0_owned
};

inline constexpr std::uint32_t kArchKeplerP0 = 2;   // GPU.ARCH_KEPLER
inline constexpr std::uint32_t kArchMaxwellP0 = 3;  // GPU.ARCH_MAXWELL

}  // namespace druta::backend
