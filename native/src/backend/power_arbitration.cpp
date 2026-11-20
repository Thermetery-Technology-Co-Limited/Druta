// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/power_arbitration.h"

#include <algorithm>
#include <iterator>
#include <set>
#include <utility>

namespace druta::backend {

namespace {

constexpr std::uint32_t kInfo = 0x2080A618;
constexpr std::uint32_t kStatus = 0x2080A619;
constexpr std::uint32_t kControl = 0x2080A61A;

// _GEOMETRY: (base, stride, command, size) in its dict order, and the index of the command in
// PowerLayout::sizes / commands (kPowerGetCommands order).
struct Geometry {
    const char* name;
    std::uint32_t base, stride, command, size;
    std::size_t slot;
};
constexpr Geometry kInfoGeometry{"info", 0xCC, 0x104, kInfo, 20512, 0};
constexpr Geometry kStatusGeometry{"status", 0x9C, 0x1730, kStatus, 397048, 1};
constexpr Geometry kControlGeometry{"control", 0x14, 0xC4, kControl, 13876, 2};
constexpr Geometry kGeometry[] = {kInfoGeometry, kStatusGeometry, kControlGeometry};

const char* method_name(std::uint32_t method) {
    switch (method) {
        case 0: return "minimum";
        case 1: return "maximum";
        case 2: return "sum";
        default: return nullptr;
    }
}

ArbitrationVector unavailable_vector(const std::string& reason) {
    ArbitrationVector v;
    v.reason = reason;
    return v;
}

std::uint32_t u32_at(const std::uint8_t* bytes, std::size_t offset) {
    return static_cast<std::uint32_t>(bytes[offset]) | static_cast<std::uint32_t>(bytes[offset + 1]) << 8 |
           static_cast<std::uint32_t>(bytes[offset + 2]) << 16 | static_cast<std::uint32_t>(bytes[offset + 3]) << 24;
}

std::pair<std::uint32_t, std::uint32_t> layout_pair(const PowerLayout& layout, const Geometry& g) {
    if (g.command == kInfo) return {layout.info_base, layout.info_stride};
    if (g.command == kStatus) return {layout.status_base, layout.status_stride};
    return {layout.control_base, layout.control_stride};
}

// _words: the length refusal before the value refusal, as Python checks them.
std::string words_error(const ArbitrationWords& words, const Geometry& g) {
    if (!words.present || words.count != g.size / 4 || (words.count && !words.data))
        return std::string("The ") + g.name + " packet has an unexpected length.";
    if (!words.valid) return std::string("The ") + g.name + " packet contains a value outside unsigned 32-bit words.";
    return {};
}

ArbitrationRow decode_row(std::uint32_t index, const std::uint32_t* info, const std::uint32_t* control,
                          const std::uint32_t* status) {
    const std::size_t meta = (kInfoGeometry.base + index * kInfoGeometry.stride) / 4;
    const std::size_t request = (kControlGeometry.base + index * kControlGeometry.stride) / 4;
    const std::size_t state = (kStatusGeometry.base + index * kStatusGeometry.stride) / 4;
    const std::uint32_t header = info[meta + 1];
    ArbitrationRow row;
    row.policy = index;
    row.type = header & 255u;
    row.channel = (header >> 8) & 255u;
    row.unit_code = (header >> 16) & 255u;
    if (row.unit_code == 0) row.unit = "mW";
    else if (row.unit_code == 1) row.unit = "mA";
    if (!arbitration_row_format(info[meta] & 255u))
        row.reason = "The policy row does not declare an understood V3 or V4 table format.";
    else if (control[request] != row.type || (status[state] & 255u) != row.type)
        row.reason = "The policy types disagree between INFO, CONTROL and STATUS.";
    if (!row.reason.empty()) {
        row.host = unavailable_vector(row.reason);
        row.pmu = unavailable_vector(row.reason);
        return row;
    }
    // The common prefix ends at 0x130; no derived-class payload is decoded.
    std::uint8_t prefix[76 * 4];
    for (std::size_t w = 0; w < 76; ++w)
        for (std::size_t b = 0; b < 4; ++b) prefix[w * 4 + b] = static_cast<std::uint8_t>(status[state + w] >> (8 * b));
    row.host = decode_arbitration_vector(prefix, sizeof(prefix), 0x40);
    row.pmu = decode_arbitration_vector(prefix, sizeof(prefix), 0xE0);
    row.available = true;
    row.requested = control[request + 1];
    row.effective = status[state + 1];
    row.value = status[state + 2];
    const std::uint32_t raw_delta = u32_at(prefix, 12);
    row.delta = raw_delta > 0x7FFFFFFFu ? static_cast<std::int32_t>(raw_delta - 0x80000000u) + INT32_MIN
                                        : static_cast<std::int32_t>(raw_delta);
    const ArbitrationVector& host = row.host;
    const ArbitrationVector& pmu = row.pmu;
    if (host.available && row.requested != host.output)
        row.observations.emplace_back("The host arbitration output differs from the RM client's request.");
    if (host.available && pmu.available && host.output != pmu.output)
        row.observations.emplace_back("The returned PMU arbitration output differs from the host output.");
    if (pmu.available && row.effective != pmu.output)
        row.observations.emplace_back("The effective limit differs from the returned PMU arbitration output.");
    if (row.delta)
        row.observations.emplace_back("A signed adjustment is applied when host limits are serialized for the PMU.");
    if (host.consistent == false)
        row.observations.emplace_back("Host arbitration output does not match its returned active inputs.");
    if (pmu.consistent == false)
        row.observations.emplace_back("PMU arbitration output does not match its returned active inputs.");
    return row;
}

}  // namespace

bool arbitration_row_format(std::uint32_t version) {
    return std::find(std::begin(kArbitrationRowFormats), std::end(kArbitrationRowFormats), version) !=
           std::end(kArbitrationRowFormats);
}

std::string arbitration_geometry(const PowerLayout* layout) {
    if (!layout) return "The policy packet layout is unavailable.";
    for (const Geometry& g : kGeometry) {
        if (layout_pair(*layout, g) != std::make_pair(g.base, g.stride))
            return "This policy packet geometry has not been traced for arbitration diagnostics.";
        if (layout->sizes[g.slot] != g.size)
            return "The policy packet sizes do not match the understood arbitration layout.";
    }
    if (layout->status_mask_word && *layout->status_mask_word != 1)
        return "The policy status mask position is not understood.";
    if (layout->has_commands)
        for (const Geometry& g : kGeometry)
            if (layout->commands[g.slot] != g.command) return "The policy command mapping is not understood.";
    return {};
}

ArbitrationVector decode_arbitration_vector(const std::uint8_t* row, std::size_t row_bytes, std::size_t offset) {
    if (!row || offset > row_bytes || row_bytes - offset < 0x50)
        return unavailable_vector("The arbitration structure is truncated.");
    const std::uint8_t* vector = row + offset;
    const std::uint32_t method = vector[0];
    const std::uint32_t count = vector[1];
    const char* name = method_name(method);
    if (!name) return unavailable_vector("Arbitration method " + std::to_string(method) + " is not understood.");
    if (count > kArbitrationInputsMax)
        return unavailable_vector("The arbitration input count exceeds its eight slots.");
    ArbitrationVector v;
    std::set<std::uint32_t> clients;
    for (std::size_t i = 0; i < count; ++i) {
        v.inputs.push_back({vector[12 + 8 * i], u32_at(vector, 16 + 8 * i)});
        clients.insert(vector[12 + 8 * i]);
    }
    if (clients.size() != count) return unavailable_vector("The arbitration structure repeats a client identifier.");
    if (!v.inputs.empty()) {
        std::uint32_t computed = v.inputs.front().value;
        for (const ArbitrationInput& input : v.inputs) {
            if (method == 0) computed = std::min(computed, input.value);
            else if (method == 1) computed = std::max(computed, input.value);
        }
        if (method == 2) {
            // Both traced implementations add using 32-bit arithmetic.
            computed = 0;
            for (const ArbitrationInput& input : v.inputs) computed += input.value;
        }
        v.computed_output = computed;
    }
    v.available = true;
    v.method = name;
    v.method_code = method;
    v.count = count;
    v.output = u32_at(vector, 4);
    v.initial_value = u32_at(vector, 8);
    if (v.computed_output) v.consistent = v.output == *v.computed_output;
    return v;
}

ArbitrationReport decode_power_arbitration(const PowerLayout* layout, const ArbitrationWords& info,
                                           const ArbitrationWords& control, const ArbitrationWords& status) {
    ArbitrationReport report;
    report.reason = arbitration_geometry(layout);
    if (!report.reason.empty()) return report;
    report.reason = words_error(info, kInfoGeometry);
    if (!report.reason.empty()) return report;
    const std::uint32_t mask = info.data[1];
    std::size_t capacity = kArbitrationPoliciesMax;
    for (const Geometry& g : kGeometry) capacity = std::min<std::size_t>(capacity, (g.size - g.base) / g.stride);
    // Always 32 for the understood geometry, so a 32-bit mask cannot exceed it; kept as Python has it.
    if (capacity < 32 && (mask >> capacity) != 0) {
        report.reason = "The policy mask exceeds the understood packet capacity.";
        return report;
    }
    if (mask == 0 && !control.present && !status.present) {
        report.available = true;
        report.mask = 0u;
        return report;
    }
    report.reason = words_error(control, kControlGeometry);
    if (report.reason.empty()) report.reason = words_error(status, kStatusGeometry);
    if (!report.reason.empty()) return report;
    const std::uint32_t* c = control.data;
    const std::uint32_t* s = status.data;
    if (s[1] != mask || c[0] != 0 || c[1] != 0 || c[2] != 0 || c[3] != 255 || c[4] != mask) {
        report.reason = "The INFO, CONTROL and STATUS occupancy masks or control header disagree.";
        return report;
    }
    report.available = true;
    report.mask = mask;
    for (std::uint32_t index = 0; index < capacity; ++index)
        if (mask >> index & 1u) report.policies.push_back(decode_row(index, info.data, c, s));
    return report;
}

}  // namespace druta::backend
