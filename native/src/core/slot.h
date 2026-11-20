// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// PCI slot: the one identity NVAPI, NVML and nvtune agree on.
// Port of nvbackend.format_slot / parse_slot / same_slot; keep them identical.
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>

namespace druta {

struct PciSlot {
    std::uint32_t domain = 0;
    std::uint32_t bus = 0;
    std::uint32_t device = 0;
    std::uint32_t function = 0;
};

// The canonical spelling, byte-identical to what nvtune prints and accepts: "0000:01:00.0".
std::string format_slot(std::uint32_t domain, std::uint32_t bus, std::uint32_t device,
                        std::uint32_t function = 0);
std::string format_slot(const PciSlot& slot);

// "0000:01:00.0" -> slot, or nullopt. Also accepts NVML's 8-digit domain
// ("00000000:01:00.0"), one hex digit of function, and surrounding whitespace.
std::optional<PciSlot> parse_slot(std::string_view text);

// Do two slot strings name the same card? Compared on (bus, device) only: NVAPI
// exposes no PCI domain, so slots built from it always read 0000.
bool same_slot(std::string_view a, std::string_view b);

}  // namespace druta
