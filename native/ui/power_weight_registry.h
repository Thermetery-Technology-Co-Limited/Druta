// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <cstdint>
#include <string>
#include <vector>
namespace druta::ui {
struct PowerWeightRegistryValue {std::wstring name;uint32_t type=0;std::vector<uint8_t> bytes;};
using PowerWeightRegistrySnapshot=std::vector<PowerWeightRegistryValue>;
struct PowerWeightRegistryPatch {uint32_t offset=0;std::vector<uint8_t> before,after;};
struct PowerWeightRegistryProjection {PowerWeightRegistrySnapshot after;std::vector<uint8_t> expected_image;};
// Pure and side-effect free. REG_BINARY is type 3. Throws on malformed evidence.
PowerWeightRegistryProjection project_power_weight_registry(const PowerWeightRegistrySnapshot&,const std::vector<uint8_t>& image,uint32_t version,uint8_t oem,const std::vector<PowerWeightRegistryPatch>&);
PowerWeightRegistrySnapshot restore_power_weight_registry(const PowerWeightRegistrySnapshot& before,const PowerWeightRegistrySnapshot& after,const PowerWeightRegistrySnapshot& current);
}
