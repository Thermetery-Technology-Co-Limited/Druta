// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "profile_io.h"
#include <drutacore/drutacore.h>
namespace druta::ui {
struct FanProfile {std::string slot,uuid;dc_fan_snapshot requests{};};
// Typed requested policies/levels only; RPM and automatic target fluctuations
// are observations. All functions are pure and issue no hardware operations.
FanProfile parse_fan_profile(const ProfileJson&);
ProfileJson capture_fan_profile(const dc_gpu_identity&,const dc_fan_state&);
dc_fan_snapshot stage_fan_profile(const FanProfile&,const dc_gpu_identity&,const dc_fan_state&);
bool fan_profile_matches(const dc_fan_snapshot&,const dc_fan_state&);
}
