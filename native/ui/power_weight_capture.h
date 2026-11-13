// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "power_relationships.h"
#include "power_weight_registry.h"
#include "voltage_evidence_provider.h"
#include "voltage_scaffold_receipt.h"
namespace druta::ui {
struct PowerWeightCapture {
 PowerRelationshipIdentity identity;dc_target target{};dc_gpu_identity adapter{};PowerRelationshipLayout layout;
 std::vector<uint8_t> image,info;
 VoltagePolicyAvailability image_availability=VoltagePolicyAvailability::unknown,info_availability=VoltagePolicyAvailability::unknown,registry_availability=VoltagePolicyAvailability::unknown;
 std::string image_message,info_message,registry_message;
 std::optional<VoltageScaffoldWindowsTarget> windows;
 std::optional<PowerWeightRegistrySnapshot> registry;
};
struct PowerWeightCaptureApi {
 VoltageEvidenceApi evidence;
 decltype(&dc_cl_abi) abi=nullptr;
 decltype(&dc_cl_rm) rm=nullptr;
 std::function<VoltageScaffoldWindowsTarget(const dc_gpu_identity&,const dc_target&)> resolve_windows;
 std::function<PowerWeightRegistrySnapshot(const VoltageScaffoldWindowsTarget&)> registry_read;
};
// Zero-I/O construction. Each explicit capture is locked and target-bracketed.
// Component failures are independent; identity/target errors abort the call.
// False excludes ALL Windows-key and registry lookup.
std::function<PowerWeightCapture(dc_gpu*,bool include_registry)> make_power_weight_capture(PowerWeightCaptureApi,const dc_gpu_identity&);
}
