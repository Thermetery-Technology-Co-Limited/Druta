// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "power_weight_capture.h"
#include <optional>
namespace druta::ui {
inline constexpr size_t power_weight_receipt_max_bytes=8u*1024u*1024u;
struct PowerWeightReceipt {
 std::string id,uuid,phase="prepared",message;
 PowerRelationshipIdentity identity;
 VoltageScaffoldWindowsTarget windows;
 PowerRelationshipLayout layout;
 std::vector<uint8_t> image,info;
 std::vector<PowerWeightRequest> requests;
 PowerWeightRegistrySnapshot before;
 std::vector<std::wstring> issued_names;
 bool refresh_requested=false,uncertain=false,restoring=false;
};
struct PowerWeightReceiptDerived {
 PowerWeightPlan plan;
 PowerWeightRegistrySnapshot after;
};
PowerWeightReceipt make_weight_receipt(const PowerWeightCapture&,const std::vector<PowerWeightRequest>&);
PowerWeightReceiptDerived derive_weight_receipt(const PowerWeightReceipt&);
std::string encode_weight_receipt(const PowerWeightReceipt&);
PowerWeightReceipt decode_weight_receipt(const std::string&);
// Explicit direct-child store. Empty directory selects LOCALAPPDATA/Druta/power-weights.
// create writes/flushed receipt first, then atomically publishes one active pointer per slot+UUID.
// save requires the same active receipt; clear only removes a matching pointer, never the journal.
std::wstring power_weight_receipt_directory();
std::wstring power_weight_receipt_path(const std::wstring& directory,const std::string& id);
void create_weight_receipt(const std::wstring& directory,const PowerWeightReceipt&);
void save_weight_receipt(const std::wstring& directory,const PowerWeightReceipt&);
PowerWeightReceipt read_weight_receipt(const std::wstring& directory,const std::string& id);
std::optional<PowerWeightReceipt> discover_weight_receipt(const std::wstring& directory,const std::string& slot,const std::string& uuid);
bool clear_active_weight_receipt(const std::wstring& directory,const PowerWeightReceipt&);
}
