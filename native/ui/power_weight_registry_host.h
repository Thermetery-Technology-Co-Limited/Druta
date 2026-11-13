// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "power_weight_registry.h"
#include "voltage_scaffold_receipt.h"
#include "voltage_limits_service.h"
#include <functional>
namespace druta::ui {
// Injectable single snapshot reader. It receives only a validated display-class key.
using PowerWeightRegistryReader=std::function<PowerWeightRegistrySnapshot(const std::wstring&)>;
// Explicit read only; returns two matching observations, no registry mutation.
PowerWeightRegistrySnapshot read_power_weight_registry(const VoltageScaffoldWindowsTarget&,const PowerWeightRegistryReader& reader={});
}
