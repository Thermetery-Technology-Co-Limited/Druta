// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "monitor_model.h"
#include "shunt_model.h"
namespace druta::ui {
// Both skins consume identical per-field availability/freshness semantics.
void draw_monitor_tiles(const SessionSnapshot& snapshot, const ShuntCorrection& correction, bool classic = false);
void draw_monitor_details(const SessionSnapshot& snapshot, bool classic = false);
}
