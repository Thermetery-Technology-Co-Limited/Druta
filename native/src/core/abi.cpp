// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The C ABI: thin, exception-free wrappers over the C++ core.
#include "drutacore/drutacore.h"

#include <cstring>
#include <string>
#include <vector>

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

}  // extern "C"
