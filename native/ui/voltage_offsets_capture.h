// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "voltage_offsets_service.h"
namespace druta::ui {
// Factory performs no I/O; explicit capture owns the recursive GPU lock.
std::function<VoltageOffsetSnapshot(dc_gpu*)> make_voltage_offsets_capture(
 VoltageEvidenceApi,const dc_gpu_identity&,VoltageScaffoldHostApi host={});
}
