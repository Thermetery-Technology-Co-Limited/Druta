// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The C ABI of drutacore/perf_diagnostics.h.
#include <cstring>

#include "backend/perf_diagnostics.h"
#include "core/abi_internal.h"
#include "core/mutex.h"

namespace {

using druta::backend::CapturedTransport;
using druta::backend::PerfMask;
using druta::backend::PerfRequest;

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

bool error_ok(const dc_error* error) { return !error || error->size == sizeof(dc_error); }

void put_transport(const CapturedTransport& t, dc_rm_transport* out) {
    dc_rm_transport r{};
    r.size = sizeof(dc_rm_transport);
    r.captured = 1;
    r.header_count = t.header_count;
    for (std::size_t i = 0; i < 17; ++i) r.header[i] = t.header[i];
    r.fields = dc_escape_fields{t.fields.hAdapter, t.fields.hDevice, t.fields.Type, t.fields.Flags, t.fields.hContext};
    *out = r;
}

}  // namespace

extern "C" {

DC_API dc_status DC_CALL dc_perf_layouts(dc_perf_layout* out, uint32_t capacity, uint32_t* count) {
    if (!count || (!out && capacity)) return DC_E_ARGUMENT;
    const auto& table = druta::backend::perf_layouts();
    for (uint32_t i = 0; i < capacity && i < table.size(); ++i)
        if (out[i].size != sizeof(dc_perf_layout)) return DC_E_ARGUMENT;
    for (uint32_t i = 0; i < capacity && i < table.size(); ++i) {
        const druta::backend::PerfGroupLayout& g = table[i];
        dc_perf_layout row{};
        row.size = sizeof(dc_perf_layout);
        DC_COPY(row.name, std::string(g.name));
        row.info_id = g.info_id;
        row.control_id = g.control_id;
        row.info_command = g.info_command;
        row.control_command = g.control_command;
        row.info_size = g.info_size;
        row.control_size = g.control_size;
        row.capacity = g.capacity;
        row.mask_bytes = g.mask_bytes;
        row.info_offset = g.info_offset;
        row.info_stride = g.info_stride;
        row.control_offset = g.control_offset;
        row.control_stride = g.control_stride;
        out[i] = row;
    }
    *count = static_cast<uint32_t>(table.size());
    return DC_OK;
}

DC_API int32_t DC_CALL dc_perf_probe_architecture(uint32_t architecture) {
    return druta::backend::probe_architecture(architecture) ? 1 : 0;
}

DC_API int32_t DC_CALL dc_perf_get_allowed(uint32_t command, uint32_t size) {
    return druta::backend::perf_get_allowed(command, size) ? 1 : 0;
}

DC_API dc_status DC_CALL dc_perf_transport(dc_gpu* handle, dc_rm_transport* out, dc_error* error) {
    if (!handle || !out || out->size != sizeof(dc_rm_transport) || !error_ok(error)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    druta::backend::PerfTransportResult r = handle->perf.transport();
    if (!r.transport) {
        put_error(r.error, error);
        return DC_E_FAILED;
    }
    put_transport(*r.transport, out);
    put_error(druta::backend::RmError{}, error);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_perf_bind(dc_gpu* handle, dc_error* error) {
    if (!handle || !error_ok(error)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    druta::backend::TargetResult r = handle->perf.bind();
    if (!r.target) {
        put_error(r.error, error);
        return DC_E_FAILED;
    }
    put_error(druta::backend::RmError{}, error);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_perf_get(dc_gpu* handle, uint32_t command, uint32_t size, const uint32_t* header,
                                     uint32_t header_count, const dc_escape_fields* fields, const uint8_t* mask,
                                     uint32_t mask_size, int32_t has_mask, uint8_t* out, uint32_t capacity,
                                     dc_error* error) {
    if (!handle || !error_ok(error) || (!header && header_count) || (!fields && header_count) ||
        has_mask < DC_PERF_MASK_NONE || has_mask > DC_PERF_MASK_OTHER ||
        (has_mask == DC_PERF_MASK_BYTES && !mask && mask_size) || (!out && capacity))
        return DC_E_ARGUMENT;
    const PerfMask kind = has_mask == DC_PERF_MASK_BYTES ? PerfMask::bytes
                          : has_mask == DC_PERF_MASK_OTHER ? PerfMask::other
                                                           : PerfMask::none;
    druta::LockGuard guard(handle->gpu.lock());
    // The core takes the output buffer up front, so an allowed size it cannot hold is refused
    // before the driver is touched.
    if (druta::backend::perf_get_allowed(command, size) && capacity < size) return DC_E_BUFFER;
    std::optional<PerfRequest> given;
    if (fields) {
        given.emplace();
        given->header = header;
        given->header_count = header_count;
        given->fields = druta::backend::EscapeFields{fields->hAdapter, fields->hDevice, fields->Type, fields->Flags,
                                                     fields->hContext};
    }
    druta::backend::PerfGetResult r =
        handle->perf.get(command, size, given ? &*given : nullptr, kind, mask, mask_size);
    if (r.bad_transport) return DC_E_ARGUMENT;  // Python's TypeError (None) or IndexError (too long)
    if (!r.payload) {
        put_error(r.error, error);
        return DC_E_FAILED;
    }
    if (!r.payload->empty()) std::memcpy(out, r.payload->data(), r.payload->size());
    put_error(druta::backend::RmError{}, error);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_perf_reset(dc_gpu* handle) {
    if (!handle) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    handle->perf.reset();
    return DC_OK;
}

}  // extern "C"
