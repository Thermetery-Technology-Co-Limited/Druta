// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "profile_selection.h"
#include <optional>
#include <drutacore/drutacore.h>
namespace druta::ui {
struct PythonSavingRequest {
    bool clock=false;uint32_t family=0,index=0,value=0;
    std::string slot,uuid;
    uint32_t architecture=0;
    std::optional<uint32_t> device_id,subsystem_id;
};
// One released schema-2 lowpower_requests or pcie_speed document. Pure.
PythonSavingRequest parse_python_saving_profile(const ProfileJson&);
struct SavingProfileSplit {std::vector<ProfileJson> power_saving,clock_gating;};
// A singular released scope or a collection made entirely of these scopes.
// Validates the complete source, identities and duplicate targets first.
SavingProfileSplit split_saving_profile(const ProfileJson&);
std::string saving_profile_target(const PythonSavingRequest&);
void match_python_saving_target(const PythonSavingRequest&,const dc_gpu_identity&,const std::optional<dc_target>&);
}
