// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "monitor_model.h"
#include "shunt_model.h"
namespace druta::ui {
// Both skins consume identical per-field availability/freshness semantics.
void draw_monitor_tiles(const SessionSnapshot& snapshot, const ShuntCorrection& correction, bool classic = false);
void draw_monitor_details(const SessionSnapshot& snapshot, bool classic = false);
// Classic: call after the last fixed-height item below the details (the History
// header), so the next frame's band budget leaves exactly that much room for it.
void note_classic_monitor_page_end();
}
