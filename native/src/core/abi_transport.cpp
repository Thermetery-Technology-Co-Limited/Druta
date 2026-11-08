// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The C ABI of the RM escape transport (DESIGN-TRANSPORT.md 3.7), increment 1:
// the gdi32 capability record and the raw escape.
#include <cstring>

#include "backend/gdi.h"
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

DC_API dc_status DC_CALL dc_escape_capability_get(dc_escape_capability* out) {
    if (!out || out->size != sizeof(dc_escape_capability)) return DC_E_ARGUMENT;
    const druta::backend::EscapeCapability& cap = druta::backend::escape_capability();
    dc_escape_capability c{};
    c.size = sizeof(dc_escape_capability);
    c.gdi32_loaded = cap.gdi32_loaded ? 1 : 0;
    c.escape = cap.escape ? 1 : 0;
    c.query_adapter_info = cap.query_adapter_info ? 1 : 0;
    c.close_adapter = cap.close_adapter ? 1 : 0;
    c.enum_adapters2 = cap.enum_adapters2 ? 1 : 0;
    c.enum_adapters = cap.enum_adapters ? 1 : 0;
    c.open_from_display_name = cap.open_from_display_name ? 1 : 0;
    c.open_from_hdc = cap.open_from_hdc ? 1 : 0;
    c.complete = cap.complete() ? 1 : 0;
    c.method = static_cast<int32_t>(cap.method);
    c.patch_bytes = sizeof(void*) == 8 ? 14 : 5;
    DC_COPY(c.detail, cap.detail);
    *out = c;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_rm_escape_raw(dc_gpu* handle, uint8_t* packet, size_t size, const dc_escape_fields* fields,
                                          int32_t* ntstatus, dc_error* error) {
    if (!handle || !fields || !ntstatus || (!packet && size) || size > 0xFFFFFFFFu) return DC_E_ARGUMENT;
    if (error && error->size != sizeof(dc_error)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    druta::backend::EscapeFields f{fields->hAdapter, fields->hDevice, fields->Type, fields->Flags, fields->hContext};
    druta::backend::EscapeResult r = druta::backend::escape_raw(handle->gpu, packet, static_cast<uint32_t>(size), f);
    if (r.outcome != druta::backend::EscapeOutcome::issued) {
        put_error(r.why, error);
        return DC_E_UNAVAILABLE;
    }
    *ntstatus = static_cast<int32_t>(r.ntstatus);
    if (error) put_error(druta::backend::RmError{}, error);
    return DC_OK;
}

}  // extern "C"
