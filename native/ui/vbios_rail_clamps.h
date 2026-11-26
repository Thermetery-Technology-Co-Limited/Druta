// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <cstdint>
#include <optional>
#include <vector>

namespace druta::ui {
// Upper bounds stored in the direct, absolute-voltage REL and ALT equations.
// Neither value describes the driver's current effective ceiling.
struct VbiosRailClamps {
    bool sram = false;
    std::optional<float> rel_uv, alt_uv;
};

std::vector<VbiosRailClamps> inspect_vbios_rail_clamps(const std::vector<uint8_t>& image,
                                                      uint32_t expected_device_id);
}
