// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Layer 0 of the RM escape transport: gdi32's D3DKMT* exports as
// capabilities, adapter identification by PCI address, and one raw escape.
// Port of nvbackend.GPU._legacy_clk_gdi and _legacy_clk_escape
// (DESIGN-TRANSPORT.md 2.2 and 3.3).
//
// Adapter handles are never cached: every escape enumerates again, matches
// exactly one adapter at the paired PCI slot, and closes every handle it
// opened. Export presence is the only capability check; nothing here reads a
// Windows or driver version.
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>

#include "backend/gpu.h"
#include "backend/rm_error.h"

namespace druta::backend {

enum class AdapterMethod : std::int32_t { none = 0, enum2 = 1, enum_ = 2, gdi_display_name = 3, hdc = 4 };

// Computed once per loader: export presence does not change while a process runs.
struct EscapeCapability {
    bool gdi32_loaded = false;
    bool escape = false, query_adapter_info = false, close_adapter = false;
    bool enum_adapters2 = false, enum_adapters = false, open_from_display_name = false, open_from_hdc = false;
    AdapterMethod method = AdapterMethod::none;  // this increment: Enum2 only (main's path)
    bool complete() const { return escape && query_adapter_info && close_adapter && method != AdapterMethod::none; }
    std::string detail;  // "" when complete, else exactly what is missing
};
const EscapeCapability& escape_capability();

// The 2.2 slot rule: "0+:BB:DD.F" with two hex digits each and a function 0-7,
// nothing else (stricter than druta::parse_slot).
struct EscapeSlot {
    std::uint32_t bus, device, function;
};
std::optional<EscapeSlot> escape_slot(std::string_view slot);

// The escape fields reused verbatim from a capture; hAdapter is replaced per escape.
struct EscapeFields {
    std::uint32_t hAdapter = 0, hDevice = 0, Type = 0, Flags = 0, hContext = 0;
};

enum class EscapeOutcome { issued, unavailable };
struct EscapeResult {
    EscapeOutcome outcome = EscapeOutcome::unavailable;
    long ntstatus = 0;  // when issued
    RmError why;        // when unavailable: fixed (capability) or transient (enumeration, identity)
};

// _legacy_clk_escape(packet, fields): the caller's buffer is used in place.
EscapeResult escape_raw(Gpu& gpu, void* packet, std::uint32_t size, const EscapeFields& fields);

}  // namespace druta::backend
