// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "profile_selection.h"
namespace druta::ui {
// Structural inspection only; selected operations require fresh service preflight.
ProfileSelection describe_voltage_policy_profile(const ProfileJson&);
ProfileJson select_voltage_policy_profile(const ProfileJson&,const std::vector<std::string>&);
ProfileSelection describe_power_integral_profile(const ProfileJson&);
ProfileJson select_power_integral_profile(const ProfileJson&,const std::vector<std::string>&);
}
