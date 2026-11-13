/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "perf_view_model.h"
namespace druta::ui {
// Draw only. Returns an explicit refresh intent; never calls the GPU.
bool draw_perf_diagnostics(const PerfViewModel& model);
}
