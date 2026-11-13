// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "power_weight_capture.h"
#include "power_weight_receipt.h"
namespace druta::ui {
struct PowerWeightsApi {
 std::function<PowerWeightCapture(dc_gpu*,bool include_registry)> capture;
 std::function<void(const PowerWeightReceipt&)> create_receipt;
 std::function<std::optional<PowerWeightReceipt>(const std::string&,const std::string&)> discover_receipt;
 std::function<PowerWeightReceipt(const std::string&)> read_receipt;
};
struct PowerWeightRow {
 uint32_t index=0,type=0,target=0;std::optional<uint16_t> weight_raw;
 std::optional<PowerRelationshipTarget> target_metadata;
 bool eligible_for_preview=false,stale=false;
 VoltagePolicyAvailability availability=VoltagePolicyAvailability::unknown;std::string message;
};
struct PowerWeightsPreview {
 struct State;bool ok=false,no_change=false;std::string message;
 std::vector<PowerWeightSelection> selected;std::vector<PowerWeightRegistryValue> registry_added;
 std::string image_sha256,expected_image_sha256;VoltageScaffoldWindowsTarget windows;
 std::shared_ptr<const State> state;
};
struct PowerWeightsData {
 uint64_t revision=0;dc_gpu_identity identity{};
 std::vector<PowerWeightRow> rows;PowerRelationshipReport report;PowerWeightImage image;
 VoltagePolicyAvailability image_availability=VoltagePolicyAvailability::unknown,info_availability=VoltagePolicyAvailability::unknown,registry_availability=VoltagePolicyAvailability::unknown;
 bool image_stale=false,info_stale=false,registry_stale=false;
 std::string image_message,info_message,registry_message,message;
 std::shared_ptr<const PowerWeightsPreview> preview;
 std::shared_ptr<const PowerWeightReceipt> receipt;
 std::string receipt_message;
};
struct PowerWeightsResult {bool ok=false;std::string message;};
class PowerWeightsService {
public:
 PowerWeightsService(PowerWeightsApi,dc_gpu*,const dc_gpu_identity&);
 ~PowerWeightsService(); // No writes, registration, restart or activation.
 const PowerWeightsData& read();const PowerWeightsData& state()const;
 PowerWeightsPreview preview(const std::vector<PowerWeightRequest>&);
 PowerWeightsResult validate_preview(const PowerWeightsPreview&);
 PowerWeightsResult inspect_receipt(const std::string& id={});
 PowerWeightsResult prepare_activation();
private:struct Impl;std::unique_ptr<Impl> impl_;
};
}
