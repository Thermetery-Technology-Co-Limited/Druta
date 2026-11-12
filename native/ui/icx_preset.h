/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "i2c_driver.h"
#include "profile_io.h"
namespace druta::ui {
// All three functions are pure. Capture requires a complete, current iCX view.
// Parsing validates the legacy schema; it never accesses or writes a device.
ProfileJson capture_icx_preset(const ControllerView& view);
std::vector<ControllerRequest> icx_preset_requests(const ProfileJson& document, bool xoc);
std::string describe_icx_preset(const ProfileJson& document);
}
