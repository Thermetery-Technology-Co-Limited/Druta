// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The C ABI of drutacore/legacy_p0.h.
#include "backend/legacy_p0.h"
#include "core/abi_internal.h"
#include "core/mutex.h"

namespace {

// No exception crosses the C boundary; only allocation can throw here.
template <typename F>
dc_status guarded(F&& body) {
    try {
        return body();
    } catch (...) {
    }
    return DC_E_FAILED;
}

}  // namespace

extern "C" {

DC_API dc_status DC_CALL dc_legacy_p0_supported(dc_gpu* handle, int32_t* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        *out = handle->legacy_p0.supported() ? 1 : 0;
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_legacy_p0_owned(dc_gpu* handle, int32_t* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        *out = handle->legacy_p0.owned() ? 1 : 0;
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_legacy_p0_hold(dc_gpu* handle, dc_outcome* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        // LegacyP0::hold takes the handle lock per step and releases it across each wait.
        druta::backend::Outcome outcome = handle->legacy_p0.hold();
        druta::abi::put_outcome(outcome, out);
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_legacy_p0_release(dc_gpu* handle, dc_outcome* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        druta::backend::Outcome outcome = handle->legacy_p0.release();
        druta::abi::put_outcome(outcome, out);
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_legacy_p0_request(dc_gpu* handle, int32_t* acquired, dc_outcome* out) {
    if (!handle || !acquired || !out) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        bool took = false;
        druta::backend::Outcome outcome{false, {}};
        try {
            outcome = handle->legacy_p0.request(took);
        } catch (...) {
            // request() sets ownership before its call and can still throw afterwards (building
            // its message): report the ownership taken so the caller's release runs.
            *acquired = took ? 1 : 0;
            throw;
        }
        *acquired = took ? 1 : 0;
        druta::abi::put_outcome(outcome, out);
        return DC_OK;
    });
}

}  // extern "C"
