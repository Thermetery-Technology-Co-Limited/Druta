// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <cstdint>
#include <string>
#include <vector>

namespace druta::ui {
// Identification of stored ROM fields only. The inventory has no writer.
struct VbiosOverrideFamily {
    std::string name, observation;
    bool identified = false;
};
struct VbiosOverrideInventory {
    std::string error;
    std::vector<VbiosOverrideFamily> families;
};
VbiosOverrideInventory inspect_vbios_override_inventory(const std::vector<uint8_t>& image,
                                                        uint32_t expected_device_id, uint32_t architecture = 0);
} // namespace druta::ui
