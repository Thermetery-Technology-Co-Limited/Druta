// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The C ABI of the RM escape transport (DESIGN-TRANSPORT.md 3.7): the gdi32
// capability record and the raw escape (layer 0), the hook captures and the
// per-GPU transport cache (layers 1 and 2), and the hook's test seams.
#include <cstring>

#include "backend/exports.h"
#include "backend/gdi.h"
#include "backend/hook.h"
#include "backend/rm_transport.h"
#include "core/abi_internal.h"
#include "core/mutex.h"

namespace {

using druta::backend::CapturedTransport;
using druta::backend::RmTransport;

void put_transport(const std::optional<CapturedTransport>& t, dc_rm_transport* out) {
    dc_rm_transport r{};
    r.size = sizeof(dc_rm_transport);
    if (t) {
        r.captured = 1;
        r.header_count = t->header_count;
        for (std::size_t i = 0; i < 17; ++i) r.header[i] = t->header[i];
        r.fields = dc_escape_fields{t->fields.hAdapter, t->fields.hDevice, t->fields.Type, t->fields.Flags,
                                    t->fields.hContext};
    }
    *out = r;
}

std::optional<CapturedTransport> from_transport(const dc_rm_transport& in) {
    if (!in.captured) return std::nullopt;
    CapturedTransport t;
    t.header_count = in.header_count;
    for (std::size_t i = 0; i < 17; ++i) t.header[i] = in.header[i];
    t.fields = druta::backend::EscapeFields{in.fields.hAdapter, in.fields.hDevice, in.fields.Type, in.fields.Flags,
                                            in.fields.hContext};
    return t;
}

std::optional<CapturedTransport>* cache_slot(RmTransport& rm, int32_t kind) {
    switch (kind) {
        case DC_TRANSPORT_POWER: return &rm.power;
        case DC_TRANSPORT_LEGACY_CLK: return &rm.legacy_clk;
        default: return nullptr;
    }
}

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


// ---- layers 1 and 2: captures and the transport cache ------------------------------------------

DC_API dc_status DC_CALL dc_rm_transport_get(dc_gpu* handle, int32_t kind, dc_rm_transport* out) {
    if (!handle || !out || out->size != sizeof(dc_rm_transport)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    std::optional<CapturedTransport>* slot = cache_slot(handle->rm, kind);
    if (!slot) return DC_E_ARGUMENT;
    put_transport(*slot, out);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_rm_transport_set(dc_gpu* handle, int32_t kind, const dc_rm_transport* transport) {
    if (!handle || !transport || transport->size != sizeof(dc_rm_transport)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    std::optional<CapturedTransport>* slot = cache_slot(handle->rm, kind);
    if (!slot) return DC_E_ARGUMENT;
    *slot = from_transport(*transport);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_rm_transport_reset(dc_gpu* handle, int32_t kind) {
    if (!handle) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    std::optional<CapturedTransport>* slot = cache_slot(handle->rm, kind);
    if (!slot) return DC_E_ARGUMENT;
    slot->reset();
    if (kind == DC_TRANSPORT_POWER) handle->rm.layout_choice.reset();
    return DC_OK;
}

DC_API dc_status DC_CALL dc_rm_observed_get(dc_gpu* handle, dc_rm_observed* out) {
    if (!handle || !out || out->size != sizeof(dc_rm_observed)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    const druta::backend::Observed& o = handle->rm.observed;
    dc_rm_observed r{};
    r.size = sizeof(dc_rm_observed);
    r.seen = o.seen ? 1 : 0;
    r.command_id = o.command_id;
    r.packet_bytes = o.packet_bytes;
    r.parameter_bytes = o.parameter_bytes;
    *out = r;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_rm_layout_choice_get(dc_gpu* handle, int32_t* has_choice, uint32_t* choice) {
    if (!handle || !has_choice || !choice) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    *has_choice = handle->rm.layout_choice ? 1 : 0;
    *choice = handle->rm.layout_choice.value_or(0u);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_rm_layout_choice_set(dc_gpu* handle, int32_t has_choice, uint32_t choice) {
    if (!handle) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    handle->rm.layout_choice = druta::abi::optional_u32(has_choice, choice);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_rm_capture(dc_gpu* handle, int32_t kind, dc_rm_transport* out) {
    if (!handle || !out || out->size != sizeof(dc_rm_transport)) return DC_E_ARGUMENT;
    if (kind != DC_TRANSPORT_POWER && kind != DC_TRANSPORT_LEGACY_CLK) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    std::optional<CapturedTransport> t =
        kind == DC_TRANSPORT_POWER ? handle->rm.capture_power() : handle->rm.capture_legacy_clk();
    put_transport(t, out);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_rm_profile_supported(dc_gpu* handle, int32_t* supported) {
    if (!handle || !supported) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    *supported = handle->rm.profile_supported() ? 1 : 0;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_rm_power_transport(dc_gpu* handle, dc_rm_transport* out, dc_error* error) {
    if (!handle || !out || out->size != sizeof(dc_rm_transport)) return DC_E_ARGUMENT;
    if (error && error->size != sizeof(dc_error)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    druta::backend::TransportResult r = handle->rm.power_transport();
    if (!r.transport) {
        put_error(r.error, error);
        return DC_E_FAILED;
    }
    put_transport(r.transport, out);
    if (error) put_error(druta::backend::RmError{}, error);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_rm_power_layouts(dc_gpu* handle, dc_power_layout* out, uint32_t capacity, uint32_t* count,
                                             dc_error* error) {
    if (!handle || !count || (!out && capacity)) return DC_E_ARGUMENT;
    if (error && error->size != sizeof(dc_error)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    druta::backend::LayoutsResult r = handle->rm.power_layouts();
    if (r.layouts.empty()) {
        put_error(r.error, error);
        *count = 0;
        return DC_E_FAILED;
    }
    const uint32_t total = static_cast<uint32_t>(r.layouts.size());
    const uint32_t written = total < capacity ? total : capacity;
    for (uint32_t i = 0; i < written; ++i)
        if (out[i].size != sizeof(dc_power_layout)) return DC_E_ARGUMENT;
    for (uint32_t i = 0; i < written; ++i) druta::abi::put_power_layout(*r.layouts[i], &out[i]);
    *count = total;
    if (error) put_error(druta::backend::RmError{}, error);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_legacy_clk_records(dc_gpu* handle, uint32_t records[2][82], int32_t* available) {
    if (!handle || !records || !available) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    auto rows = handle->vflock.legacy_clk_limit_records();
    *available = rows ? 1 : 0;
    std::memset(records, 0, sizeof(uint32_t) * 2 * 82);
    if (rows)
        for (std::size_t i = 0; i < 2 && i < rows->size(); ++i)
            for (std::size_t k = 0; k < 82 && k < (*rows)[i].size(); ++k) records[i][k] = (*rows)[i][k];
    return DC_OK;
}

DC_API dc_status DC_CALL dc_rail_write_locked(dc_gpu* handle, const dc_rail_write_request* request,
                                              dc_rail_write_result* result) {
    if (!handle || !request || !result || request->size != sizeof(dc_rail_write_request) ||
        result->size != sizeof(dc_rail_write_result) || request->record_count > 2)
        return DC_E_ARGUMENT;
    druta::rail::Record records[2] = {};
    for (uint32_t i = 0; i < request->record_count; ++i) {
        records[i].rail = request->records[i].rail;
        for (std::size_t k = 0; k < druta::rail::kRecordValueCount; ++k) records[i].values[k] = request->records[i].values[k];
    }
    druta::LockGuard guard(handle->gpu.lock());
    druta::backend::RailWriteResult r = druta::backend::rail_write_locked(
        handle->gpu, request->control_version, request->rail_mask, records, request->record_count, request->boost);
    dc_rail_write_result out{};
    out.size = sizeof(dc_rail_write_result);
    out.done = r.done ? 1 : 0;
    out.has_status = r.status ? 1 : 0;
    out.status = r.status.value_or(0u);
    DC_COPY(out.error, r.error);
    if (r.protocol) {
        out.has_protocol = 1;
        out.get_command = r.protocol->get_command;
        out.set_command = r.protocol->set_command;
        out.packet_size = r.protocol->packet_size;
        out.getter_version = r.protocol->getter_version;
    }
    out.observed_count = static_cast<uint32_t>(r.observed.size() < 8 ? r.observed.size() : 8);
    for (uint32_t i = 0; i < out.observed_count; ++i) DC_COPY(out.observed[i], r.observed[i]);
    *result = out;
    return DC_OK;
}

// ---- test seams ----------------------------------------------------------------------------------

DC_API int32_t DC_CALL dc_hook_test_entry(void* escape) {
    // Only meaningful while a test resolver serves gdi32; on a real image the thunk is reached
    // through the patched D3DKMTEscape, never through this export.
    if (!druta::backend::test_resolver_installed()) return static_cast<int32_t>(0xC000000Du);  // STATUS_INVALID_PARAMETER
    return static_cast<int32_t>(druta::backend::hook_entry(escape));
}

DC_API dc_status DC_CALL dc_hook_test_state(int32_t* installed, int32_t* patched, uint32_t* calls) {
    if (!installed || !patched || !calls) return DC_E_ARGUMENT;
    druta::backend::HookState state = druta::backend::hook_state();
    *installed = state.installed ? 1 : 0;
    *patched = state.patched ? 1 : 0;
    *calls = state.calls;
    return DC_OK;
}

DC_API void DC_CALL dc_hook_test_fail_protect(int32_t fail) { druta::backend::hook_test_fail_protect(fail != 0); }

DC_API dc_status DC_CALL dc_hook_thunk(uint64_t address, uint64_t target, uint32_t pointer_bytes, uint8_t out[14],
                                       uint32_t* size) {
    if (!out || !size || (pointer_bytes != 4 && pointer_bytes != 8)) return DC_E_ARGUMENT;
    druta::backend::Thunk t = druta::backend::thunk_bytes(address, target, pointer_bytes);
    std::memcpy(out, t.bytes, 14);
    *size = static_cast<uint32_t>(t.size);
    return DC_OK;
}

}  // extern "C"
