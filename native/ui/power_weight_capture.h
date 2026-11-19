// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "power_relationships.h"
#include "pascal_clock_table.h"
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
 std::optional<PascalTimingSample> timing_sample;
 VoltagePolicyAvailability timing_availability=VoltagePolicyAvailability::unknown;
 std::string timing_sample_message;
};
// Require physical controller confirmation at the reviewed operating point.
void verify_pascal_timing_sample(const PowerWeightCapture&,const PascalTimingSample& expected);
struct PowerWeightCaptureApi {
 VoltageEvidenceApi evidence;
 decltype(&dc_gpu_vbios_target_bind) clock_bind=nullptr;
 decltype(&dc_cl_abi) abi=nullptr;
 decltype(&dc_cl_rm) rm=nullptr;
 std::function<VoltageScaffoldWindowsTarget(const dc_gpu_identity&,const dc_target&)> resolve_windows;
 std::function<PowerWeightRegistrySnapshot(const VoltageScaffoldWindowsTarget&)> registry_read;
 decltype(&dc_gpu_read_telemetry) telemetry=nullptr;
 std::function<std::array<uint32_t,2>(dc_gpu*)> timing_config;
};
// Zero-I/O construction. Each explicit capture is locked and target-bracketed.
// Component failures are independent; identity/target errors abort the call.
// False excludes ALL Windows-key and registry lookup.
std::function<PowerWeightCapture(dc_gpu*,bool include_registry)> make_power_weight_capture(PowerWeightCaptureApi,const dc_gpu_identity&,bool clock_only=false);
}
