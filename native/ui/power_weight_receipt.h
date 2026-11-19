// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "power_weight_capture.h"
#include "pascal_clock_table.h"
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
 std::vector<PascalClockEdit> clocks;
 // A revision retains the original image and derives its pre-restart image from the previous edits.
 std::vector<PascalClockEdit> previous_clocks;
 bool is_pascal_clocks() const { return !clocks.empty(); }
 PowerWeightRegistrySnapshot before;
 std::vector<std::wstring> issued_names;
 bool refresh_requested=false,uncertain=false,restoring=false;
};
enum class PascalTimingPhase {original, previous, applied};
// Before, pre-revision, or requested physical P0 controller words.
std::optional<PascalTimingSample> receipt_timing_sample(const PowerWeightReceipt&, PascalTimingPhase);
struct PowerWeightReceiptDerived {
 PowerWeightPlan plan;
 PascalClockPlan clock_plan;
 std::vector<uint8_t> clock_source_image;
 PowerWeightRegistrySnapshot after;
};
PowerWeightReceipt make_weight_receipt(const PowerWeightCapture&,const std::vector<PowerWeightRequest>&);
PowerWeightReceipt make_pascal_clock_receipt(const PowerWeightCapture&,const std::vector<PascalClockEdit>&);
PowerWeightReceipt revise_pascal_clock_receipt(const PowerWeightCapture&,const PowerWeightReceipt& active,
                                               const std::vector<PascalClockEdit>&);
PowerWeightReceiptDerived derive_weight_receipt(const PowerWeightReceipt&);
std::string encode_weight_receipt(const PowerWeightReceipt&);
PowerWeightReceipt decode_weight_receipt(const std::string&);
// Explicit direct-child store. Empty directory selects LOCALAPPDATA/Druta/power-weights.
// create writes/flushed receipt first, then atomically publishes one active pointer per slot+UUID.
// replace compares the full predecessor journal, then publishes a fresh Pascal revision.
// save requires the same active receipt; clear only removes a matching pointer, never the journal.
std::wstring power_weight_receipt_directory();
std::wstring power_weight_receipt_path(const std::wstring& directory,const std::string& id);
std::wstring power_weight_operation_lock_path(const std::wstring& directory,const std::string& id);
void create_weight_receipt(const std::wstring& directory,const PowerWeightReceipt&);
void replace_pascal_clock_receipt(const std::wstring& directory,const PowerWeightReceipt& active,
                                  const PowerWeightReceipt& replacement);
// Caller holds the receipt operation lock. A canceled preview cannot retire a started trial.
bool unused_clock_preparation(const PowerWeightReceipt&);
void abandon_prepared_clock_receipt(const std::wstring& directory,const PowerWeightReceipt& prepared);
void save_weight_receipt(const std::wstring& directory,const PowerWeightReceipt&);
PowerWeightReceipt read_weight_receipt(const std::wstring& directory,const std::string& id);
std::optional<PowerWeightReceipt> discover_weight_receipt(const std::wstring& directory,const std::string& slot,const std::string& uuid);
std::vector<PowerWeightReceipt> active_clock_receipts(const std::wstring& directory, const std::string& slot = {});
bool clear_active_weight_receipt(const std::wstring& directory,const PowerWeightReceipt&);
}
