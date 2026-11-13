// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <vector>
namespace druta::ui {
struct PowerRelationshipIdentity {std::string slot;uint32_t architecture=0,device_id=0,subsystem_id=0;};
struct PowerRelationshipLayout {uint32_t info_command=0x2080a618,info_size=20512,policy_base=0xcc,policy_stride=0x104;};
struct PowerRelationshipTarget {uint32_t policy=0,type=0,channel=0,unit=0,minimum=0,default_value=0,maximum=0;}; // unit:0 mW,1 mA
struct PowerRelationshipRecord {uint32_t index=0,type=0,target=0;std::optional<uint16_t> weight_raw;std::optional<PowerRelationshipTarget> target_metadata;std::string target_reason;};
struct PowerRelationshipSource {uint32_t policy=0,first=0,last=0;std::optional<bool> enabled;std::vector<uint32_t> relationships;bool available=false;std::string reason;};
struct PowerRelationshipLink {uint32_t policy=0,relationship=0;std::optional<uint32_t> target;bool available=false;std::string reason;};
struct PowerRelationshipReport {
 bool available=false;std::string reason;uint32_t policy_mask=0,relationship_mask=0;
 std::vector<PowerRelationshipRecord> relationships;
 std::vector<PowerRelationshipSource> source_references,range_references;
 std::vector<PowerRelationshipLink> linked_references;std::vector<uint32_t> source_uninterpreted_policies;
};
PowerRelationshipReport decode_power_relationships(const PowerRelationshipLayout&,const std::vector<uint8_t>& info);
struct PowerWeightImagePolicy {uint32_t index=0,bios_type=0;std::optional<PowerRelationshipTarget> metadata;bool qualified=false;std::string reason;};
struct PowerWeightImageRelationship {uint32_t index=0,kind=0,target=0,offset=0;uint16_t weight_raw=0;bool qualified=false;std::string reason;};
struct PowerWeightImage {
 bool ok=false,relationships_available=false;std::string message,relationship_message;
 uint32_t selector_version=0;uint8_t selector_oem=0;
 uint32_t table_offset=0,table_size=0,table_version=0,header_size=0,stride=0,count=0,relationship_count=0;
 std::vector<PowerWeightImagePolicy> policies;std::vector<PowerWeightImageRelationship> relationships;
};
PowerWeightImage inspect_power_weight_image(const std::vector<uint8_t>& image);
uint16_t power_weight_raw(double multiplier); // Throws unless exact unsigned Q4.12.
struct PowerWeightRequest {uint32_t index=0;double multiplier=1;std::optional<uint16_t> expected_raw;};
struct PowerWeightPatch {uint32_t offset=0;std::vector<uint8_t> before,after;};
struct PowerWeightSelection {uint32_t index=0,offset=0,target=0;uint16_t before_raw=0,after_raw=0;PowerRelationshipTarget metadata;}; // offset is record base; weight is +2
struct PowerWeightPlan {
 struct State;bool ok=false;std::string message;
 PowerRelationshipIdentity identity;std::vector<PowerWeightPatch> patches;std::vector<PowerWeightSelection> selected;
 std::shared_ptr<const std::vector<uint8_t>> original_image,expected_image,original_info,expected_info;
 std::shared_ptr<const State> state;
};
PowerWeightPlan plan_power_relationship_weights(const std::vector<uint8_t>& image,const PowerRelationshipIdentity&,
 const PowerRelationshipLayout&,const std::vector<uint8_t>& info,const std::vector<PowerWeightRequest>&);
struct PowerWeightVerification {bool ok=false;std::string message;bool image_verified=false;};
// Exact INFO preservation is mandatory. Supplying an image additionally
// verifies complete planned/original image equality; no archive hash repair.
PowerWeightVerification verify_power_relationship_weights(const PowerWeightPlan&,const PowerRelationshipIdentity&,
 const PowerRelationshipLayout&,const std::vector<uint8_t>& info,const std::vector<uint8_t>* image=nullptr,bool original=false);
}
