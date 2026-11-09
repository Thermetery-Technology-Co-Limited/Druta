// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/perf_diagnostics.h"

#include <cstdio>
#include <cstring>
#include <string>

#include "backend/gdi.h"

namespace druta::backend {

namespace {

RmError perf_error(RmErrorKind kind, std::string message) {
    RmError e;
    e.kind = kind;
    e.message = std::move(message);
    return e;
}

// f"0x{value:08X}" (padded) or f"0x{value:X}".
std::string hex(std::uint32_t value, bool padded) {
    char text[16];
    if (padded)
        std::snprintf(text, sizeof text, "0x%08X", static_cast<unsigned>(value));
    else
        std::snprintf(text, sizeof text, "0x%X", static_cast<unsigned>(value));
    return text;
}

constexpr std::uint32_t kHeaderWords = 17;
constexpr std::uint32_t kPrologueBytes = kHeaderWords * 4;  // 68: the payload starts after the header
constexpr std::uint32_t kMaskOffset = 72;                   // the request mask's byte offset in the packet

}  // namespace

// ---- perf_layouts -----------------------------------------------------------------------------

const std::array<PerfGroupLayout, 3>& perf_layouts() {
    static const std::array<PerfGroupLayout, 3> table{{
        {"limits", 0x4D2C0A9C, 0xA59BE705, 0x2080A022, 0x2080A024, 0x7A8C, 0x16850, 512, 64, 0x288, 0x3C, 0x50, 0xB4},
        {"cf_controller", 0x153DE751, 0x3BD390E8, 0x2080A0C8, 0x2080E0CA, 0x6A0C, 0x588C, 32, 4, 0xC, 0x350, 0xC,
         0x2C4},
        {"cf_policy", 0x1CAC3865, 0x69DE2C54, 0x2080A0CC, 0x2080A0CE, 0x230, 0x88, 32, 4, 0x30, 0x10, 8, 4},
    }};
    return table;
}

bool perf_get_allowed(std::uint32_t command, std::uint32_t size) {
    for (const PerfGroupLayout& layout : perf_layouts())
        if ((command == layout.info_command && size == layout.info_size) ||
            (command == layout.control_command && size == layout.control_size))
            return true;
    return false;
}

const PerfGroupLayout* perf_layout_for_command(std::uint32_t command) {
    for (const PerfGroupLayout& layout : perf_layouts())
        if (command == layout.info_command || command == layout.control_command) return &layout;
    return nullptr;
}

// ---- the reader -------------------------------------------------------------------------------

PerfTransportResult PerfDiagnostics::transport() {
    PerfTransportResult out;
    LayoutsResult layouts = rm_.power_layouts();
    if (layouts.layouts.empty()) {
        // CurrentLimitUnsupported is "unsupported"; every other ValueError a failed read.
        out.error = perf_error(
            layouts.error.kind == RmErrorKind::unsupported ? RmErrorKind::unsupported : RmErrorKind::transient,
            layouts.error.message);
        return out;
    }
    if (!rm_.power) {
        out.error = perf_error(RmErrorKind::transient, "No captured paired RM getter transport");
        return out;
    }
    // Python also checks that every header word is an int in 0..0xFFFFFFFF and that the fields are
    // exactly the five escape fields: both hold by construction here (std::uint32_t words, the
    // EscapeFields struct). header_count is a fixture's stored length.
    const CapturedTransport& t = *rm_.power;
    if (t.header_count != kHeaderWords || t.header[14] != kPerfTransportCommand ||
        static_cast<std::uint64_t>(t.header[2]) != std::uint64_t{kPrologueBytes} + t.header[15]) {
        // Explicit, as the plan asks: an RM26 (0x20802612) transport is a validated power transport
        // but not one the PERF GETs are understood to share.
        out.error = perf_error(RmErrorKind::unsupported, "PERF diagnostics need the validated modern RM transport");
        return out;
    }
    out.transport = t;
    return out;
}

PerfGetResult PerfDiagnostics::get(std::uint32_t command, std::uint32_t size, const CapturedTransport* transport,
                                   PerfMask mask_kind, const std::uint8_t* mask, std::uint32_t mask_size) {
    PerfGetResult out;
    if (!perf_get_allowed(command, size)) {
        out.error = perf_error(RmErrorKind::invalid, "Only understood read-only PERF commands are allowed");
        return out;
    }
    const PerfGroupLayout& layout = *perf_layout_for_command(command);
    if (command == layout.control_command) {
        if (mask_kind != PerfMask::bytes || mask_size != layout.mask_bytes) {
            out.error = perf_error(RmErrorKind::invalid, "PERF CONTROL GET requires an exact INFO mask");
            return out;
        }
    } else if (mask_kind != PerfMask::none) {
        out.error = perf_error(RmErrorKind::invalid, "PERF INFO GET does not accept a request mask");
        return out;
    }
    TargetResult bound = binding_.bind();
    if (!bound.target) {
        out.error = bound.error;
        return out;
    }
    if (!transport) {
        out.no_transport = true;
        return out;
    }
    const std::uint32_t words = kHeaderWords + size / 4;
    const std::uint32_t packet_bytes = words * 4;
    std::vector<std::uint32_t> packet(words, 0);
    // Python copies every header word it was given; a stored header is at most 17 words here.
    const std::uint32_t count = transport->header_count < kHeaderWords ? transport->header_count : kHeaderWords;
    for (std::uint32_t i = 0; i < count; ++i) packet[i] = transport->header[i];
    packet[2] = packet_bytes;
    packet[14] = command;
    packet[15] = size;
    packet[16] = 0;
    if (mask_kind == PerfMask::bytes && mask_size)
        std::memcpy(reinterpret_cast<std::uint8_t*>(packet.data()) + kMaskOffset, mask, mask_size);

    EscapeResult escape = escape_raw(gpu_, packet.data(), packet_bytes, transport->fields);
    bound = binding_.bind();
    if (!bound.target) {
        out.error = bound.error;
        return out;
    }
    // _legacy_clk_escape returns None when no escape was issued; None != 0 takes this branch too.
    if (escape.outcome != EscapeOutcome::issued || escape.ntstatus != 0) {
        rm_.power.reset();
        rm_.layout_choice.reset();
        const bool issued = escape.outcome == EscapeOutcome::issued;
        out.error = perf_error(RmErrorKind::transient, "PERF GET transport failed (NTSTATUS " +
                                                           (issued ? std::to_string(escape.ntstatus) : std::string("None")) +
                                                           ")");
        if (issued) out.error.ntstatus = escape.ntstatus;
        return out;
    }
    if (packet[16] != 0) {
        const std::uint32_t rm = packet[16];
        out.error = perf_error(rm == 0x1F || rm == 0x56 ? RmErrorKind::unsupported : RmErrorKind::transient,
                               "PERF GET " + hex(command, true) + " rejected (RM " + hex(rm, false) + ")");
        out.error.ntstatus = 0;
        out.error.rm_status = rm;
        return out;
    }
    if (packet[2] != packet_bytes || packet[14] != command || packet[15] != size) {
        out.error = perf_error(RmErrorKind::malformed, "PERF response changed its command or parameter geometry");
        out.error.ntstatus = 0;
        return out;
    }
    const std::uint8_t* bytes = reinterpret_cast<const std::uint8_t*>(packet.data());
    out.payload.emplace(bytes + kPrologueBytes, bytes + packet_bytes);
    return out;
}

}  // namespace druta::backend
