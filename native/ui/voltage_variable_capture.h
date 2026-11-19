// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "voltage_limits_service.h"
namespace druta::ui {
struct VoltageVariableCapture { dc_target target{}; std::vector<uint8_t> bytes; };
// Caller holds the GPU lock. No writes; transient failures get one complete retry.
VoltageVariableCapture capture_voltage_variables(const VoltagePolicyApi&, dc_gpu*, const dc_gpu_identity&);
}
