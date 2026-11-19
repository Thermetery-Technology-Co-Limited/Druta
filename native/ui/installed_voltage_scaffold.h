// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "vbios_voltage_graph.h"
#include "voltage_limits_service.h"
#include <memory>
#include <utility>
namespace druta::ui {
struct InstalledVoltageScaffoldHead {
 uint32_t rail=0,index=0,next=0xffff,variable=0,output=2,selector_offset=0,row_offset=0;
 VbiosVoltageRole role=VbiosVoltageRole::rel;int64_t value_uv=0;
};
struct InstalledVoltageScaffoldChange {uint32_t offset=0;uint8_t before=0,after=0;};
struct InstalledVoltageScaffoldPlan {
 struct State;bool ok=false;std::string message;
 std::vector<InstalledVoltageScaffoldHead> heads;
 std::vector<InstalledVoltageScaffoldChange> changes;
 std::shared_ptr<const std::vector<uint8_t>> original_image,planned_image;
 std::shared_ptr<const VbiosVoltageGraph> original_graph;
 std::shared_ptr<const State> state;
};
struct InstalledVoltageScaffoldResult {
 bool ok=false,complete_image_match=false;std::string message;
 std::vector<InstalledVoltageScaffoldHead> heads;
 std::shared_ptr<const VbiosVoltageGraph> current_graph;
};
// Pure planning only. Returned changed bytes describe an understood scaffold;
// they do not authorize registry/image writes or opaque packet replay.
InstalledVoltageScaffoldPlan plan_installed_voltage_scaffold(
 const std::vector<uint8_t>& original,const VoltageEquationPackets& before,
 const std::vector<uint8_t>& variable_info,
 const std::vector<std::pair<uint32_t,VbiosVoltageRole>>& selected);
// Matches released verify_loaded: target VFE/rail spans must be exact; other
// image bytes may differ but current ownership must still parse. The optional
// strict flag adds verify_activation's complete planned-image equality.
InstalledVoltageScaffoldResult recognize_installed_voltage_scaffold(
 const InstalledVoltageScaffoldPlan&,const std::vector<uint8_t>& current,
 const VoltageEquationPackets& loaded,bool require_complete_image=false);
InstalledVoltageScaffoldResult recognize_installed_voltage_scaffold(
 const std::vector<uint8_t>& original,const VoltageEquationPackets& before,
 const std::vector<uint8_t>& variable_info,
 const std::vector<std::pair<uint32_t,VbiosVoltageRole>>& selected,
 const std::vector<uint8_t>& current,const VoltageEquationPackets& loaded,
 bool require_complete_image=false);
}
