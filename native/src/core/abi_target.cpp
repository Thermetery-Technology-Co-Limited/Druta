// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The C ABI of the paired target binding (increment A1).
#include <cstring>

#include "backend/target.h"
#include "core/abi_internal.h"
#include "core/mutex.h"

namespace {

void put_error(const druta::backend::RmError& why, dc_error* out) {
    if (!out) return;
    dc_error e{};
    e.size = sizeof(dc_error);
    e.kind = static_cast<int32_t>(why.kind);
    e.fixed = why.fixed ? 1 : 0;
    e.has_ntstatus = why.ntstatus ? 1 : 0;
    e.ntstatus = why.ntstatus ? static_cast<int32_t>(*why.ntstatus) : 0;
    e.rm_status = why.rm_status;
    DC_COPY(e.message, why.message);
    *out = e;
}

}  // namespace

extern "C" {

DC_API dc_status DC_CALL dc_gpu_target_bind(dc_gpu* handle, int32_t client_power, dc_target* out, dc_error* error) {
    if (!handle || !out || out->size != sizeof(dc_target) || (error && error->size != sizeof(dc_error))) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    druta::backend::TargetResult r = client_power ? handle->target.bind_client_power() : handle->target.bind();
    if (!r.target) {
        put_error(r.error, error);
        return DC_E_FAILED;
    }
    dc_target t{};
    t.size = sizeof(dc_target);
    DC_COPY(t.slot, r.target->slot);
    t.architecture = r.target->architecture;
    t.device_id = r.target->device_id;
    t.subsystem_id = r.target->subsystem_id;
    t.handle = static_cast<uint64_t>(reinterpret_cast<std::uintptr_t>(r.target->handle));
    *out = t;
    put_error(druta::backend::RmError{}, error);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_target_reset(dc_gpu* handle) {
    if (!handle) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    handle->target.reset();
    return DC_OK;
}

DC_API int32_t DC_CALL dc_nvapi_interface_present(const dc_gpu* handle, uint32_t id) {
    if (!handle) return 0;
    return handle->gpu.nvapi.interface(id) != nullptr ? 1 : 0;
}

}  // extern "C"
