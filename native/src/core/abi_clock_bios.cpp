// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The C ABI of drutacore/clock_bios.h.
#include <cstring>
#include <string>

#include "backend/clock_bios.h"
#include "core/abi_internal.h"
#include "core/mutex.h"

namespace {

using druta::backend::ClockBiosFailure;
using druta::backend::ClockIdentity;

static_assert(DC_CLOCK_BIOS_CONTROL_SIZE == druta::backend::kClockControlSize, "CONTROL size");
static_assert(DC_CLOCK_BIOS_CONTROL_VERSION == druta::backend::kClockControlVersion, "CONTROL version");

void put_message(int32_t kind, const std::string& message, dc_error* out) {
    if (!out) return;
    dc_error e{};
    e.size = sizeof(dc_error);
    e.kind = kind;
    DC_COPY(e.message, message);
    *out = e;
}

// DC_E_FAILED for a PerfDiagnosticError, DC_E_UNRECOGNIZED for a ClockBiosError.
dc_status put_failure(const ClockBiosFailure& failure, dc_error* out) {
    if (failure.perf_error) {
        put_message(static_cast<int32_t>(failure.error.kind), failure.error.message, out);
        return DC_E_FAILED;
    }
    put_message(DC_ERR_NONE, failure.error.message, out);
    return DC_E_UNRECOGNIZED;
}

bool error_ok(const dc_error* error) { return !error || error->size == sizeof(dc_error); }

// The C callback behind ClockIdentityDiffers: the bound identity as a dc_target.
struct IdentityCheck {
    dc_clock_bios_identity_fn differs;
    void* context;
};

std::int32_t identity_differs(void* context, const ClockIdentity& bound) {
    const IdentityCheck& check = *static_cast<const IdentityCheck*>(context);
    dc_target t{};
    t.size = sizeof(dc_target);
    DC_COPY(t.slot, bound.slot);
    t.architecture = bound.architecture;
    t.device_id = bound.device_id;
    t.subsystem_id = bound.subsystem_id;
    return check.differs(check.context, &t);
}

}  // namespace

extern "C" {

DC_API dc_status DC_CALL dc_clock_bios_control(dc_gpu* handle, dc_clock_bios_identity_fn differs, void* context,
                                               uint32_t* mask, uint8_t* payload, uint32_t capacity,
                                               dc_error* error) {
    if (!handle || !mask || (!payload && capacity) || !error_ok(error)) return DC_E_ARGUMENT;
    if (capacity < DC_CLOCK_BIOS_CONTROL_SIZE) return DC_E_BUFFER;
    IdentityCheck check{differs, context};
    druta::LockGuard guard(handle->gpu.lock());
    // A NULL callback is an identity that differs from every binding.
    druta::backend::ClockControlResult r =
        handle->clock_bios.control_once(differs ? identity_differs : nullptr, &check);
    if (!r.ok()) return put_failure(r.failure, error);
    *mask = r.mask;
    std::memcpy(payload, r.payload.data(), r.payload.size());
    put_message(DC_ERR_NONE, std::string(), error);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_clock_bios_control_shape(const char* payload_hex, uint32_t length, uint64_t mask,
                                                     dc_clock_bios_requests* out, dc_error* error) {
    if (!out || out->size != sizeof(dc_clock_bios_requests) || (!payload_hex && length) || !error_ok(error))
        return DC_E_ARGUMENT;
    const druta::backend::ClockShapeResult r = druta::backend::clock_control_shape_hex(payload_hex, length, mask);
    if (!r.requests) {
        ClockBiosFailure failure;
        failure.error.message = r.error;
        return put_failure(failure, error);
    }
    dc_clock_bios_requests result{};
    result.size = sizeof(dc_clock_bios_requests);
    result.present = r.requests->present;
    for (uint32_t i = 0; i < DC_CLOCK_BIOS_CONTROL_CAPACITY; ++i) result.khz[i] = r.requests->khz[i];
    *out = result;
    put_message(DC_ERR_NONE, std::string(), error);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_clock_bios_reset(dc_gpu* handle) {
    if (!handle) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    handle->clock_bios.reset();
    return DC_OK;
}

DC_API dc_status DC_CALL dc_clock_bios_held(dc_gpu* handle, int32_t* held) {
    if (!handle || !held) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    *held = handle->clock_bios.held() ? 1 : 0;
    return DC_OK;
}

}  // extern "C"
