// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The C ABI of drutacore/perf_diagnostics.h.
#include <cstring>
#include <exception>
#include <string>

#include "backend/perf_diagnostics.h"
#include "core/abi_internal.h"
#include "core/mutex.h"
#include "core/sha256.h"

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

void put_message(int32_t kind, const std::string& message, dc_error* out) {
    if (!out) return;
    dc_error e{};
    e.size = sizeof(dc_error);
    e.kind = kind;
    DC_COPY(e.message, message);
    *out = e;
}

// No exception crosses the C boundary (as abi_clock_bios.cpp's guarded): an allocation failure in
// the decoder, the packets or the copies is DC_E_FAILED, reported as transient when there is an
// error out. A handle lock taken inside body is released by its guard while unwinding.
template <typename F>
dc_status guarded(dc_error* error, F&& body) {
    try {
        return body();
    } catch (const std::exception& e) {
        try {
            put_message(DC_ERR_TRANSIENT, std::string("PERF reader failed: ") + e.what(), error);
        } catch (...) {
        }
    } catch (...) {
        try {
            put_message(DC_ERR_TRANSIENT, "PERF reader failed", error);
        } catch (...) {
        }
    }
    return DC_E_FAILED;
}

void put_transport(const CapturedTransport& t, dc_rm_transport* out) {
    dc_rm_transport r{};
    r.size = sizeof(dc_rm_transport);
    r.captured = 1;
    r.header_count = t.header_count;
    for (std::size_t i = 0; i < 17; ++i) r.header[i] = t.header[i];
    r.fields = dc_escape_fields{t.fields.hAdapter, t.fields.hDevice, t.fields.Type, t.fields.Flags, t.fields.hContext};
    *out = r;
}

void put_snapshot(const druta::backend::PerfSnapshot& snap, dc_perf_snapshot* out) {
    const uint32_t size = out->size;
    std::memset(out, 0, sizeof(*out));
    out->size = size;
    const druta::backend::PerfGroupLayout& g = *snap.layout;
    DC_COPY(out->group, std::string(g.name));
    out->capacity = g.capacity;
    out->mask_bytes = g.mask_bytes;
    std::memcpy(out->mask, snap.mask.data(), snap.mask.size());
    out->info_command = g.info_command;
    out->control_command = g.control_command;
    out->info_bytes = g.info_size;
    out->control_bytes = g.control_size;
    DC_COPY(out->info_sha256, snap.info_sha256);
    out->has_control_sha256 = snap.control_sha256 ? 1 : 0;
    if (snap.control_sha256) DC_COPY(out->control_sha256, *snap.control_sha256);
    out->flag_count = static_cast<uint32_t>(snap.flags.size());
    for (std::size_t i = 0; i < snap.flags.size() && i < DC_PERF_MAX_FLAGS; ++i) {
        dc_perf_flag& f = out->flags[i];
        DC_COPY(f.label, std::string(snap.flags[i].label));
        f.raw = snap.flags[i].raw;
        f.has_boolean = snap.flags[i].boolean ? 1 : 0;
        f.boolean = snap.flags[i].boolean.value_or(false) ? 1 : 0;
    }
    out->target_count = static_cast<uint32_t>(snap.targets.size());
    for (std::size_t i = 0; i < snap.targets.size() && i < DC_PERF_TARGET_COUNT; ++i) {
        const druta::backend::PerfTarget& t = snap.targets[i];
        dc_perf_target& r = out->targets[i];
        DC_COPY(r.field, std::string(t.field));
        r.has_index = t.index ? 1 : 0;
        r.index = t.index.value_or(0u);
        r.present = t.present ? 1 : 0;
        r.has_flag_raw = t.flag_raw ? 1 : 0;
        r.flag_raw = t.flag_raw.value_or(0);
        r.has_issue = t.issue ? 1 : 0;
        if (t.issue) DC_COPY(r.issue, std::string(t.issue));
    }
    out->issue_count = static_cast<uint32_t>(snap.issues.size());
    for (std::size_t i = 0; i < snap.issues.size() && i < DC_PERF_MAX_FLAGS; ++i)
        DC_COPY(out->issues[i], snap.issues[i]);
    out->entry_count = static_cast<uint32_t>(snap.entries.size());
    for (std::size_t i = 0; i < snap.entries.size() && i < DC_PERF_MAX_ENTRIES; ++i) {
        const druta::backend::PerfEntry& e = snap.entries[i];
        dc_perf_entry& r = out->entries[i];
        r.index = e.index;
        r.type = e.type;
        r.control_type = e.control_type;
        r.info_bytes = static_cast<uint32_t>(e.info_raw.size());
        r.control_bytes = static_cast<uint32_t>(e.control_raw.size());
        std::memcpy(r.info_raw, e.info_raw.data(), std::min<std::size_t>(e.info_raw.size(), sizeof r.info_raw));
        std::memcpy(r.control_raw, e.control_raw.data(),
                    std::min<std::size_t>(e.control_raw.size(), sizeof r.control_raw));
        r.has_driver_label = e.driver_label ? 1 : 0;
        if (e.driver_label) DC_COPY(r.driver_label, *e.driver_label);
        r.issue_count = static_cast<uint32_t>(e.issues.size());
        if (!e.issues.empty()) DC_COPY(r.issue, std::string(e.issues.front()));
        r.has_policy = e.policy ? 1 : 0;
        r.role_bytes_raw[0] = e.role_bytes_raw[0];
        r.role_bytes_raw[1] = e.role_bytes_raw[1];
        r.role_id = e.role_id;
        r.controller_mask = e.controller_mask;
        r.has_registry_flag = e.registry_controlled_flag_raw ? 1 : 0;
        r.registry_controlled_flag_raw = e.registry_controlled_flag_raw.value_or(0);
    }
}

druta::backend::RmError invalid(std::string message) {
    druta::backend::RmError e;
    e.kind = druta::backend::RmErrorKind::invalid;
    e.message = std::move(message);
    return e;
}

}  // namespace

extern "C" {

DC_API dc_status DC_CALL dc_perf_layouts(dc_perf_layout* out, uint32_t capacity, uint32_t* count) {
    if (!count || (!out && capacity)) return DC_E_ARGUMENT;
    const auto& table = druta::backend::perf_layouts();
    for (uint32_t i = 0; i < capacity && i < table.size(); ++i)
        if (out[i].size != sizeof(dc_perf_layout)) return DC_E_ARGUMENT;
    return guarded(nullptr, [&]() -> dc_status {
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
    });
}

DC_API int32_t DC_CALL dc_perf_probe_architecture(uint32_t architecture) {
    try {  // allocates nothing; guarded like every entry point
        return druta::backend::probe_architecture(architecture) ? 1 : 0;
    } catch (...) {
        return 0;
    }
}

DC_API int32_t DC_CALL dc_perf_get_allowed(uint32_t command, uint32_t size) {
    try {  // allocates nothing; guarded like every entry point
        return druta::backend::perf_get_allowed(command, size) ? 1 : 0;
    } catch (...) {
        return 0;
    }
}

DC_API dc_status DC_CALL dc_perf_transport(dc_gpu* handle, dc_rm_transport* out, dc_error* error) {
    if (!handle || !out || out->size != sizeof(dc_rm_transport) || !error_ok(error)) return DC_E_ARGUMENT;
    return guarded(error, [&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        druta::backend::PerfTransportResult r = handle->perf.transport();
        if (!r.transport) {
            put_error(r.error, error);
            return DC_E_FAILED;
        }
        put_transport(*r.transport, out);
        put_error(druta::backend::RmError{}, error);
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_perf_bind(dc_gpu* handle, dc_error* error) {
    if (!handle || !error_ok(error)) return DC_E_ARGUMENT;
    return guarded(error, [&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        druta::backend::TargetResult r = handle->perf.bind();
        if (!r.target) {
            put_error(r.error, error);
            return DC_E_FAILED;
        }
        put_error(druta::backend::RmError{}, error);
        return DC_OK;
    });
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
    return guarded(error, [&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        // The core takes the output buffer up front, so an allowed size it cannot hold is refused
        // before the driver is touched.
        if (druta::backend::perf_get_allowed(command, size) && capacity < size) return DC_E_BUFFER;
        std::optional<PerfRequest> given;
        if (fields) {
            given.emplace();
            given->header = header;
            given->header_count = header_count;
            given->fields = druta::backend::EscapeFields{fields->hAdapter, fields->hDevice, fields->Type,
                                                         fields->Flags, fields->hContext};
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
    });
}

DC_API dc_status DC_CALL dc_perf_reset(dc_gpu* handle) {
    if (!handle) return DC_E_ARGUMENT;
    return guarded(nullptr, [&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        handle->perf.reset();
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_perf_decode_group(const char* name, const uint8_t* info, size_t info_size,
                                              const uint8_t* control, size_t control_size, int32_t has_control,
                                              dc_perf_snapshot* out, dc_error* error) {
    if (!name || (!info && info_size) || (has_control && !control && control_size) || (!has_control && control) ||
        !out || out->size != sizeof(dc_perf_snapshot) || !error_ok(error))
        return DC_E_ARGUMENT;
    return guarded(error, [&]() -> dc_status {
        static const uint8_t empty = 0;  // a zero-length buffer passed as NULL
        druta::backend::PerfDecodeResult r = druta::backend::perf_decode_group(
            name, info ? info : &empty, info_size, has_control ? (control ? control : &empty) : nullptr, control_size);
        if (!r.snapshot) {
            put_error(r.error, error);
            return DC_E_FAILED;
        }
        put_snapshot(*r.snapshot, out);
        put_error(druta::backend::RmError{}, error);
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_perf_read_group(dc_gpu* handle, const char* name, uint32_t attempts,
                                            dc_perf_group_result* out, dc_error* error) {
    if (!handle || !name || !out || out->size != sizeof(dc_perf_group_result) ||
        out->snapshot.size != sizeof(dc_perf_snapshot) || !error_ok(error))
        return DC_E_ARGUMENT;
    return guarded(error, [&]() -> dc_status {
        const druta::backend::PerfGroupLayout* layout = druta::backend::perf_layout_named(name);
        if (!layout) {
            // The core's ASCII repr of the name, cut at the 1023 bytes dc_error.message holds: a long
            // name's text is truncated here, so the Python binding formats its own (unbounded) message.
            put_error(invalid(druta::backend::perf_unknown_group_message(name)), error);
            return DC_E_FAILED;
        }
        if (attempts < 1 || attempts > 3) {
            put_error(invalid("PERF read attempts must be between 1 and 3"), error);
            return DC_E_FAILED;
        }
        // The reader takes the handle lock per attempt (Python's `with gpu._lock` inside the loop).
        druta::backend::PerfDiagnostics::GroupRead r = handle->perf.read_group(*layout, attempts);
        const uint32_t snapshot_size = out->snapshot.size;
        out->attempts = r.attempts;
        out->has_target = r.target ? 1 : 0;
        std::memset(out->slot, 0, sizeof out->slot);
        out->architecture = 0;
        if (r.target) {
            DC_COPY(out->slot, r.target->slot);
            out->architecture = r.target->architecture;
        }
        if (r.snapshot) {
            out->snapshot.size = snapshot_size;
            put_snapshot(*r.snapshot, &out->snapshot);
            out->status = r.snapshot->entries.empty() ? DC_PERF_STATUS_EMPTY : DC_PERF_STATUS_READY;
            put_error(druta::backend::RmError{}, error);
        } else {
            out->status = DC_PERF_STATUS_FAILED;
            put_error(r.error, error);
        }
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_sha256_hex(const uint8_t* data, size_t size, char out[65]) {
    if ((!data && size) || !out) return DC_E_ARGUMENT;
    return guarded(nullptr, [&]() -> dc_status {
        static const uint8_t empty = 0;
        const std::string hex = druta::sha256_hex(data ? data : &empty, size);
        std::memcpy(out, hex.c_str(), 65);
        return DC_OK;
    });
}

}  // extern "C"
