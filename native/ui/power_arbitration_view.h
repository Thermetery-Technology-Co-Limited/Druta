/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "power_arbitration_service.h"
namespace druta::ui {
// Returns an explicit refresh intent. Drawing never calls the GPU.
// classic: released 1.9.1 "Power/current arbitration" - one paragraph, "Read
// arbitration" and the readback in a framed read-only text box.
// slot: the adapter the model was read from, for Python's "Adapter:" line.
bool draw_power_arbitration(const PowerArbitrationModel& model,bool classic=false,const std::string& slot={});
// The classic readback text, in registry_panel.py arbitration_lines' wording; native
// detail it does not print (value, initial/computed votes, STATUS packet) follows it.
std::string power_arbitration_text(const PowerArbitrationModel& model,const std::string& slot={});
}
