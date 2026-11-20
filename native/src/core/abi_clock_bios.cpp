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
static_assert(DC_CLOCK_BIOS_INFO_SIZE == druta::backend::kClockInfoSize, "INFO size");
static_assert(DC_CLOCK_BIOS_INFO_COMMAND == druta::backend::kClockInfoCommand, "INFO command");
static_assert(DC_CLOCK_BIOS_INFO_EXPORT == druta::backend::kClockInfoExport, "INFO export");
static_assert(DC_CLOCK_BIOS_INFO_CAPACITY == druta::backend::kClockInfoCapacity, "INFO capacity");

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

// No exception crosses the C boundary (as abi_client_power_controller.cpp's guarded): with an error
// out, the failure is reported there as transient.
template <typename F>
dc_status guarded(dc_error* error, F&& body) {
    try {
        return body();
    } catch (const std::exception& e) {
        try {
            put_message(DC_ERR_TRANSIENT, std::string("Clock-BIOS reader failed: ") + e.what(), error);
        } catch (...) {
        }
    } catch (...) {
        try {
            put_message(DC_ERR_TRANSIENT, "Clock-BIOS reader failed", error);
        } catch (...) {
        }
    }
    return DC_E_FAILED;
}

void put_target(const ClockIdentity& identity, dc_target* out) {
    dc_target t{};
    t.size = sizeof(dc_target);
    DC_COPY(t.slot, identity.slot);
    t.architecture = identity.architecture;
    t.device_id = identity.device_id;
    t.subsystem_id = identity.subsystem_id;
    *out = t;
}

void put_rows(const druta::backend::ClockInfoRows& rows, dc_clock_bios_rows* out) {
    std::memset(out, 0, sizeof *out);
    out->size = sizeof(dc_clock_bios_rows);
    out->present = rows.present;
    for (uint32_t i = 0; i < DC_CLOCK_BIOS_INFO_CAPACITY; ++i) {
        if (!(rows.present & (1u << i))) continue;
        const druta::backend::ClockInfoRow& r = rows.rows[i];
        dc_clock_bios_row& row = out->rows[i];
        row.type = r.type;
        row.base_type = r.base_type;
        row.api_domain_mask = r.api_domain_mask;
        row.typed = r.typed ? 1 : 0;
        row.minimum_mhz = r.minimum_mhz;
        row.maximum_mhz = r.maximum_mhz;
    }
}

// The C callback behind ClockIdentityDiffers: the bound identity as a dc_target.
struct IdentityCheck {
    dc_clock_bios_identity_fn differs;
    void* context;
};

std::int32_t identity_differs(void* context, const ClockIdentity& bound) {
    const IdentityCheck& check = *static_cast<const IdentityCheck*>(context);
    dc_target t;
    put_target(bound, &t);
    return check.differs(check.context, &t);
}

}  // namespace

extern "C" {

DC_API dc_status DC_CALL dc_clock_bios_control(dc_gpu* handle, dc_clock_bios_identity_fn differs, void* context,
                                               uint32_t* mask, uint8_t* payload, uint32_t capacity,
                                               dc_error* error) {
    if (!handle || !mask || (!payload && capacity) || !error_ok(error)) return DC_E_ARGUMENT;
    if (capacity < DC_CLOCK_BIOS_CONTROL_SIZE) return DC_E_BUFFER;
    return guarded(error, [&]() -> dc_status {
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
    });
}

DC_API dc_status DC_CALL dc_clock_bios_control_shape(const char* payload_hex, uint32_t length, uint64_t mask,
                                                     dc_clock_bios_requests* out, dc_error* error) {
    if (!out || out->size != sizeof(dc_clock_bios_requests) || (!payload_hex && length) || !error_ok(error))
        return DC_E_ARGUMENT;
    return guarded(error, [&]() -> dc_status {
        const druta::backend::ClockShapeResult r =
            druta::backend::clock_control_shape_hex(payload_hex, length, mask);
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
    });
}

DC_API dc_status DC_CALL dc_clock_bios_info(dc_gpu* handle, dc_target* identity, uint8_t* payload, uint32_t capacity,
                                            dc_error* error) {
    if (!handle || !identity || identity->size != sizeof(dc_target) || (!payload && capacity) || !error_ok(error))
        return DC_E_ARGUMENT;
    if (capacity < DC_CLOCK_BIOS_INFO_SIZE) return DC_E_BUFFER;
    return guarded(error, [&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        druta::backend::ClockInfoResult r = handle->clock_bios.info_once();
        if (!r.ok()) return put_failure(r.failure, error);
        put_target(*r.identity, identity);
        std::memcpy(payload, r.info.data(), r.info.size());
        put_message(DC_ERR_NONE, std::string(), error);
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_clock_bios_decode(const uint8_t* raw, uint32_t size, dc_clock_bios_rows* out,
                                              dc_error* error) {
    if (!out || out->size != sizeof(dc_clock_bios_rows) || (!raw && size) || !error_ok(error)) return DC_E_ARGUMENT;
    return guarded(error, [&]() -> dc_status {
        const druta::backend::ClockDecodeResult r = druta::backend::clock_info_decode(raw, size);
        if (!r.rows) {
            ClockBiosFailure failure;
            failure.error.message = r.error;
            return put_failure(failure, error);
        }
        put_rows(*r.rows, out);
        put_message(DC_ERR_NONE, std::string(), error);
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_clock_bios_read(dc_gpu* handle, dc_clock_bios_observation* out, dc_error* error) {
    if (!handle || !out || out->size != sizeof(dc_clock_bios_observation) || !error ||
        error->size != sizeof(dc_error))
        return DC_E_ARGUMENT;
    return guarded(error, [&]() -> dc_status {
        // No handle lock here: read() takes it per attempt.
        druta::backend::ClockReadResult r = handle->clock_bios.read();
        std::memset(out, 0, sizeof *out);
        out->size = sizeof(dc_clock_bios_observation);
        if (!r.observation) {
            put_message(DC_ERR_NONE, r.error, error);
            return DC_OK;
        }
        const druta::backend::ClockObservation& o = *r.observation;
        out->available = 1;
        put_target(o.identity, &out->identity);
        std::memcpy(out->info, o.info.data(), o.info.size());
        if (o.has_user_control) {
            out->has_user_control = 1;
            out->user_control_mask = o.user_control_mask;
            std::memcpy(out->user_control, o.user_control.data(), o.user_control.size());
        }
        put_rows(o.rows, &out->rows);
        out->stable_reads = 2;
        if (o.user_control_error) {
            out->has_user_control_error = 1;
            DC_COPY(out->user_control_error, *o.user_control_error);
        }
        put_message(DC_ERR_NONE, std::string(), error);
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_clock_bios_reset(dc_gpu* handle) {
    if (!handle) return DC_E_ARGUMENT;
    return guarded(nullptr, [&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        handle->clock_bios.reset();
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_clock_bios_held(dc_gpu* handle, int32_t* held) {
    if (!handle || !held) return DC_E_ARGUMENT;
    return guarded(nullptr, [&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        *held = handle->clock_bios.held() ? 1 : 0;
        return DC_OK;
    });
}

}  // extern "C"
