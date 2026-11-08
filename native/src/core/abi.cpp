// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The C ABI: thin, exception-free wrappers over the C++ core.
#include "drutacore/drutacore.h"

#include <cstring>
#include <new>
#include <string>
#include <vector>

#include "backend/exports.h"
#include "backend/gpu.h"
#include "core/rail_transport.h"
#include "core/slot.h"

extern "C" {

DC_API uint32_t DC_CALL dc_abi_version(void) { return DC_ABI_VERSION; }

DC_API const char* DC_CALL dc_status_text(dc_status status) {
    switch (status) {
        case DC_OK: return "ok";
        case DC_E_ARGUMENT: return "invalid argument";
        case DC_E_BUFFER: return "buffer too small";
        case DC_E_PARSE: return "text does not have the expected shape";
        case DC_E_UNRECOGNIZED: return "payload is not an exact known rail GET packet";
        case DC_E_MASK: return "requested_mask must select rail 0, rail 1, or both";
        case DC_E_RECORDS: return "requested_mask must exactly match the record rails";
        case DC_E_RANGE: return "value out of range";
        default: return "unknown status";
    }
}

DC_API dc_status DC_CALL dc_parse_slot(const char* text, uint32_t* domain, uint32_t* bus, uint32_t* device,
                                       uint32_t* function) {
    if (!text || !domain || !bus || !device || !function) return DC_E_ARGUMENT;
    auto slot = druta::parse_slot(text);
    if (!slot) return DC_E_PARSE;
    *domain = slot->domain;
    *bus = slot->bus;
    *device = slot->device;
    *function = slot->function;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_format_slot(uint32_t domain, uint32_t bus, uint32_t device, uint32_t function,
                                        char* buffer, size_t size, size_t* needed) {
    std::string text = druta::format_slot(domain, bus, device, function);
    if (needed) *needed = text.size() + 1;
    if (!buffer) return DC_E_ARGUMENT;
    if (size < text.size() + 1) return DC_E_BUFFER;
    std::memcpy(buffer, text.c_str(), text.size() + 1);
    return DC_OK;
}

DC_API int32_t DC_CALL dc_same_slot(const char* a, const char* b) {
    if (!a || !b) return 0;
    return druta::same_slot(a, b) ? 1 : 0;
}

// ---- voltage-rail packets -----------------------------------------------------------------------

namespace {

dc_status copy_out(const std::vector<uint8_t>& packet, uint8_t* out, size_t out_size, size_t* needed) {
    if (needed) *needed = packet.size();
    if (!out) return DC_E_ARGUMENT;
    if (out_size < packet.size()) return DC_E_BUFFER;
    if (!packet.empty()) std::memcpy(out, packet.data(), packet.size());
    return DC_OK;
}

dc_status copy_text(const std::string& text, char* buffer, size_t buffer_size, size_t* needed) {
    if (needed) *needed = text.size() + 1;
    if (!buffer) return DC_E_ARGUMENT;
    if (buffer_size < text.size() + 1) return DC_E_BUFFER;
    std::memcpy(buffer, text.c_str(), text.size() + 1);
    return DC_OK;
}

}  // namespace

DC_API uint32_t DC_CALL dc_rail_layout_count(void) {
    return static_cast<uint32_t>(druta::rail::known_layouts().size());
}

DC_API dc_status DC_CALL dc_rail_layout_at(uint32_t index, dc_rail_layout* layout) {
    const auto& layouts = druta::rail::known_layouts();
    if (!layout || index >= layouts.size()) return DC_E_ARGUMENT;
    const druta::rail::Layout& source = layouts[index];
    layout->packet_size = source.packet_size;
    layout->params_size = source.params_size;
    layout->get_command = source.get_command;
    layout->set_command = source.set_command;
    layout->mask_word = source.mask_word;
    layout->boost_word = source.boost_word;
    layout->record0 = source.record0;
    layout->record_stride = source.record_stride;
    layout->record_type = source.record_type;
    layout->valid_word = source.valid_word ? static_cast<int32_t>(*source.valid_word) : -1;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_rail_recognize(const uint8_t* payload, size_t size, uint32_t requested_mask,
                                           uint32_t* layout_index) {
    if ((!payload && size) || !layout_index) return DC_E_ARGUMENT;
    auto index = druta::rail::recognize_packet(druta::ByteView(payload, size), requested_mask);
    if (!index) return DC_E_UNRECOGNIZED;
    *layout_index = static_cast<uint32_t>(*index);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_rail_prepare_write(const uint8_t* payload, size_t size, uint32_t requested_mask,
                                               const dc_rail_record* records, size_t record_count, uint32_t boost,
                                               uint8_t* out, size_t out_size, size_t* needed) {
    if ((!payload && size) || (!records && record_count)) return DC_E_ARGUMENT;
    std::vector<druta::rail::Record> native(record_count);
    for (size_t i = 0; i < record_count; ++i) {
        native[i].rail = records[i].rail;
        for (size_t v = 0; v < 4; ++v) native[i].values[v] = records[i].values[v];
    }
    std::vector<uint8_t> packet;
    switch (druta::rail::prepare_write(druta::ByteView(payload, size), requested_mask, native.data(), record_count,
                                       boost, packet)) {
        case druta::rail::Error::none: break;
        case druta::rail::Error::mask: return DC_E_MASK;
        case druta::rail::Error::records: return DC_E_RECORDS;
        case druta::rail::Error::boost: return DC_E_RANGE;
        case druta::rail::Error::unrecognized: return DC_E_UNRECOGNIZED;
    }
    return copy_out(packet, out, out_size, needed);
}

DC_API dc_status DC_CALL dc_rail_packet_summary(const uint8_t* payload, size_t size, char* buffer, size_t buffer_size,
                                                size_t* needed) {
    if (!payload && size) return DC_E_ARGUMENT;
    return copy_text(druta::rail::packet_summary(druta::ByteView(payload, size)), buffer, buffer_size, needed);
}

// ---- GPU identity ---------------------------------------------------------------------------------

namespace {

void copy_field(char* field, size_t size, const std::string& text) {
    size_t n = text.size() < size - 1 ? text.size() : size - 1;
    std::memcpy(field, text.data(), n);
    field[n] = '\0';
}

#define DC_COPY(field, text) copy_field(field, sizeof(field), text)

}  // namespace

struct dc_gpu {
    druta::backend::Gpu gpu;
    explicit dc_gpu(const std::string& slot) : gpu(slot) {}
};

DC_API void DC_CALL dc_set_export_resolver(dc_resolve_fn resolve, void* context) {
    druta::backend::set_export_resolver(resolve, context);
}

DC_API dc_status DC_CALL dc_enumerate_gpus(dc_gpu_entry* entries, size_t capacity, size_t* count) {
    if (!count || (!entries && capacity)) return DC_E_ARGUMENT;
    std::vector<druta::backend::GpuListEntry> gpus = druta::backend::enumerate_gpus();
    *count = gpus.size();
    if (gpus.size() > capacity) return DC_E_BUFFER;
    for (size_t i = 0; i < gpus.size(); ++i) {
        if (entries[i].size != sizeof(dc_gpu_entry)) return DC_E_ARGUMENT;
        const druta::backend::GpuListEntry& g = gpus[i];
        DC_COPY(entries[i].slot, g.slot);
        DC_COPY(entries[i].name, g.name);
        DC_COPY(entries[i].uuid, g.uuid);
        entries[i].has_devid = g.devid ? 1 : 0;
        entries[i].devid = g.devid.value_or(0);
        entries[i].subsys = g.subsys.value_or(0);
        entries[i].nvml_index = g.nvml_index ? static_cast<int32_t>(*g.nvml_index) : -1;
        entries[i].has_nvapi = g.has_nvapi ? 1 : 0;
    }
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_open(const char* slot, dc_gpu** gpu) {
    if (!gpu) return DC_E_ARGUMENT;
    *gpu = new (std::nothrow) dc_gpu(slot ? slot : "");
    return *gpu ? DC_OK : DC_E_BUFFER;
}

DC_API void DC_CALL dc_gpu_close(dc_gpu* gpu) { delete gpu; }

DC_API dc_status DC_CALL dc_gpu_identity_get(const dc_gpu* handle, dc_gpu_identity* out) {
    if (!handle || !out || out->size != sizeof(dc_gpu_identity)) return DC_E_ARGUMENT;
    const druta::backend::Gpu& gpu = handle->gpu;
    out->nvapi_ok = gpu.nvapi.ok ? 1 : 0;
    out->nvml_ok = gpu.nvml.ok ? 1 : 0;
    DC_COPY(out->nvapi_error, gpu.nvapi.err_detail);
    DC_COPY(out->nvml_error, gpu.nvml.err_detail);
    DC_COPY(out->nvml_path, gpu.nvml.dll_path);
    DC_COPY(out->pairing_error, gpu.pairing_error);
    DC_COPY(out->requested_slot, gpu.requested_slot);
    DC_COPY(out->slot, gpu.slot());
    out->nvapi_selected = gpu.nvapi.selected ? 1 : 0;
    DC_COPY(out->nvapi_slot, gpu.nvapi.selected ? gpu.nvapi.selected->slot : std::string());
    out->nvapi_devid = gpu.nvapi.selected ? gpu.nvapi.selected->devid : 0;
    out->nvapi_subsys = gpu.nvapi.selected ? gpu.nvapi.selected->subsys : 0;
    out->nvml_selected = gpu.nvml.selected ? 1 : 0;
    DC_COPY(out->nvml_slot, gpu.nvml.selected ? gpu.nvml.selected->slot : std::string());
    out->nvml_has_devid = gpu.nvml.selected && gpu.nvml.selected->devid ? 1 : 0;
    out->nvml_devid = gpu.nvml.selected ? gpu.nvml.selected->devid.value_or(0) : 0;
    out->nvml_subsys = gpu.nvml.selected ? gpu.nvml.selected->subsys.value_or(0) : 0;
    out->nvml_index = gpu.nvml.selected ? static_cast<int32_t>(gpu.nvml.selected->nvml_index) : -1;
    DC_COPY(out->name, gpu.identity.name);
    DC_COPY(out->driver, gpu.identity.driver);
    DC_COPY(out->vbios, gpu.identity.vbios);
    DC_COPY(out->uuid, gpu.identity.uuid);
    out->admin = gpu.identity.admin ? 1 : 0;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_status_line(const dc_gpu* handle, char* buffer, size_t size, size_t* needed) {
    if (!handle) return DC_E_ARGUMENT;
    return copy_text(handle->gpu.status_line(), buffer, size, needed);
}

}  // extern "C"
