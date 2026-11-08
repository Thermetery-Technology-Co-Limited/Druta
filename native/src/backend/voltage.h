// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Core voltage boost (the VBIOS over-voltage headroom, 0..100 %). Port of
// nvbackend.GPU.set_voltage_boost; the reader lives in Telemetry.
#pragma once

#include <cstdint>

#include "backend/gpu.h"
#include "backend/outcome.h"

namespace druta::backend {

// Read-modify-write of NV_GPU_CLIENT_VOLT_RAILS_CONTROL_V1: the reserved
// words stay as the driver wrote them.
Outcome set_voltage_boost(Gpu& gpu, std::int64_t pct);

}  // namespace druta::backend
