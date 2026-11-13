// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "voltage_tables_service.h"
namespace druta::ui {
// Pure manual profiles of selected installed signed definition requests. No
// receipt packets or saved historical values are used or replayed.
ProfileJson capture_voltage_offsets_profile(const VoltageTablesData&,const std::vector<VoltageTableKey>&);
std::vector<VoltageTableRequest> stage_voltage_offsets_profile(const ProfileJson&,const VoltageTablesData&);
ProfileSelection describe_voltage_offsets_profile(const ProfileJson&);
ProfileJson select_voltage_offsets_profile(const ProfileJson&,const std::vector<std::string>& targets);
}
