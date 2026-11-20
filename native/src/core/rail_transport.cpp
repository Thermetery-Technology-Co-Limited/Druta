// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "core/rail_transport.h"

#include <cstdio>

namespace druta::rail {

const std::array<Layout, 3>& known_layouts() {
    static const std::array<Layout, 3> layouts = {{
        {1104, 1036, 0x2080B213, 0x2080F214, 18, 19, 20, 8, 5, 7},
        // Driver 566.36 constructs seven-dword records in nvapi64.dll at RVAs
        // 0x22DFC6..0x22E029; its type converter maps control type 0 to wire
        // type 2. See experiments/566-schema-static/README.md.
        {976, 908, 0x2080B213, 0x2080F214, 18, 19, 20, 7, 2, std::nullopt},
        {716, 648, 0x20803213, 0x20803214, 17, 18, 19, 5, 1, std::nullopt},
    }};
    return layouts;
}

bool valid_requested_mask(std::uint32_t requested_mask) {
    return requested_mask != 0 && (requested_mask & ~kSupportedRailMask) == 0;
}

namespace {

bool record_in_bounds(ByteView payload, const Layout& layout, std::uint32_t rail) {
    const std::size_t base = layout.record0 + static_cast<std::size_t>(rail) * layout.record_stride;
    if (!payload.word_in_bounds(base)) return false;
    for (std::size_t offset = 1; offset <= kRecordValueCount; ++offset)
        if (!payload.word_in_bounds(base + offset)) return false;
    if (layout.valid_word && !payload.word_in_bounds(base + *layout.valid_word)) return false;
    return true;
}

// Mirrors _validated_records: the record rails must be exactly the masked rails.
Error validate_records(std::uint32_t requested_mask, const Record* records, std::size_t record_count) {
    if (!valid_requested_mask(requested_mask)) return Error::mask;
    if (record_count && !records) return Error::records;
    std::uint32_t seen = 0;
    for (std::size_t i = 0; i < record_count; ++i) {
        const std::uint32_t rail = records[i].rail;
        if (rail > 1 || (seen & (1u << rail))) return Error::records;
        seen |= 1u << rail;
    }
    if (seen != requested_mask) return Error::records;
    return Error::none;
}

}  // namespace

std::optional<std::size_t> recognize_packet(ByteView payload, std::uint32_t requested_mask) {
    if (!payload.data && payload.size) return std::nullopt;
    if (!valid_requested_mask(requested_mask)) return std::nullopt;
    const auto& layouts = known_layouts();
    for (std::size_t index = 0; index < layouts.size(); ++index) {
        const Layout& layout = layouts[index];
        if (payload.size != layout.packet_size) continue;
        const std::size_t required[] = {kCommandWord, kParamsSizeWord, kStatusWord, layout.mask_word, layout.boost_word};
        bool in_bounds = true;
        for (std::size_t word : required) in_bounds = in_bounds && payload.word_in_bounds(word);
        if (!in_bounds) continue;
        if (payload.u32(kCommandWord) != layout.get_command) continue;
        if (payload.u32(kParamsSizeWord) != layout.params_size) continue;
        if (payload.u32(layout.mask_word) != requested_mask) continue;
        bool records_ok = true;
        for (std::uint32_t rail = 0; rail < 2; ++rail)
            if (requested_mask & (1u << rail)) records_ok = records_ok && record_in_bounds(payload, layout, rail);
        if (!records_ok) continue;
        return index;
    }
    return std::nullopt;
}

Error prepare_write(ByteView payload, std::uint32_t requested_mask, const Record* records, std::size_t record_count,
                    std::uint32_t boost, std::vector<std::uint8_t>& out) {
    // Same order as the Python module: records, boost, then the packet.
    if (Error error = validate_records(requested_mask, records, record_count); error != Error::none) return error;
    if (boost > 100) return Error::boost;
    auto index = recognize_packet(payload, requested_mask);
    if (!index) return Error::unrecognized;
    const Layout& layout = known_layouts()[*index];

    out.assign(payload.data, payload.data + payload.size);
    bool ok = put_u32(out, kCommandWord, layout.set_command) && put_u32(out, layout.mask_word, requested_mask) &&
              put_u32(out, layout.boost_word, boost);
    for (std::size_t i = 0; i < record_count && ok; ++i) {
        const std::size_t base = layout.record0 + static_cast<std::size_t>(records[i].rail) * layout.record_stride;
        ok = put_u32(out, base, layout.record_type);
        for (std::size_t offset = 0; offset < kRecordValueCount && ok; ++offset)
            ok = put_i32(out, base + 1 + offset, records[i].values[offset]);
        if (ok && layout.valid_word) ok = put_i32(out, base + *layout.valid_word, 1);
    }
    if (!ok) {  // unreachable after recognition proved every owned word in bounds
        out.clear();
        return Error::unrecognized;
    }
    return Error::none;
}

std::string packet_summary(ByteView payload) {
    char buffer[160];
    std::string text = "rail packet size=" + std::to_string(payload.size);
    if (!payload.word_in_bounds(kCommandWord) || !payload.word_in_bounds(kParamsSizeWord) ||
        !payload.word_in_bounds(kStatusWord))
        return text + " layout=unknown";
    const std::uint32_t command = *payload.u32(kCommandWord);
    const std::uint32_t params_size = *payload.u32(kParamsSizeWord);
    const std::uint32_t status = *payload.u32(kStatusWord);
    std::snprintf(buffer, sizeof buffer, " command=0x%08X params=%u status=0x%08X", command, params_size, status);
    text += buffer;
    const Layout* layout = nullptr;
    for (const Layout& candidate : known_layouts()) {
        if (payload.size == candidate.packet_size && params_size == candidate.params_size &&
            (command == candidate.get_command || command == candidate.set_command)) {
            layout = &candidate;
            break;
        }
    }
    if (!layout || !payload.word_in_bounds(layout->mask_word)) return text + " layout=unknown";
    std::snprintf(buffer, sizeof buffer, " layout=%u/%u operation=%s mask=0x%X", layout->packet_size,
                  layout->params_size, command == layout->get_command ? "get" : "set", *payload.u32(layout->mask_word));
    return text + buffer;
}

}  // namespace druta::rail
