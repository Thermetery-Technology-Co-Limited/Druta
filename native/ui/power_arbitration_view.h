/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "power_arbitration_service.h"
namespace druta::ui {
// Returns an explicit refresh intent. Drawing never calls the GPU.
bool draw_power_arbitration(const PowerArbitrationModel& model);
}
