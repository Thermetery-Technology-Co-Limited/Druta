/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "perf_view_model.h"
namespace druta::ui {
// Draw only. Returns an explicit refresh intent; never calls the GPU.
// classic: released 1.9.1 "Driver policy diagnostics" - one paragraph, "Read driver
// policies" and the readback in a framed read-only text box.
bool draw_perf_diagnostics(const PerfViewModel& model,bool classic=false);
// The classic readback text, in registry_panel.py refresh_diagnostics' wording;
// empty before the first read, as Python's box is.
std::string perf_diagnostics_text(const PerfViewModel& model);
// Native raw INFO/CONTROL capture details, shown in Classic's closed "Raw capture"
// section after the released box. Empty until a group has a snapshot.
std::string perf_diagnostics_raw_text(const PerfViewModel& model);
}
