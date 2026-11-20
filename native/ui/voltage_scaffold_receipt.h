// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "installed_voltage_scaffold.h"
#include "device_restart.h"
#include <functional>
#include <map>
namespace druta::ui {
inline constexpr size_t voltage_scaffold_receipt_max_bytes=8u*1024u*1024u;
struct VoltageScaffoldWindowsTarget {std::string slot,instance_id,driver_key;};
struct VoltageScaffoldReceipt {
 std::string slot,source_sha256;VoltageScaffoldWindowsTarget windows_target;
 std::vector<uint8_t> original_image;VoltageEquationPackets before;
 std::vector<std::pair<uint32_t,VbiosVoltageRole>> requests;
 std::vector<InstalledVoltageScaffoldHead> recorded_heads;
 // Saved bookkeeping only. Fresh recognition determines current head values.
 std::map<std::string,int64_t> historical_values;
};
struct VoltageScaffoldDriverValue {uint32_t type=0;std::wstring value;};
struct VoltageScaffoldWindowsApi {
 std::function<std::vector<DeviceRestartTarget>()> enumerate;
 std::function<VoltageScaffoldDriverValue(const std::wstring&)> driver_value;
};
// Read-only pairing/PnP/registry lookup; no device restart or registry mutation.
VoltageScaffoldWindowsTarget resolve_voltage_scaffold_windows_target(const dc_gpu_identity&,const dc_target&,const VoltageScaffoldWindowsApi&);
VoltageScaffoldWindowsTarget resolve_voltage_scaffold_windows_target(const dc_gpu_identity&,const dc_target&);
// Throws on invalid, oversized or mismatched evidence. Parsing does not prove
// installation: caller must reconstruct and freshly recognize the scaffold.
VoltageScaffoldReceipt parse_voltage_scaffold_receipt(const std::string& utf8,const dc_target& freshly_bound,const VoltageScaffoldWindowsTarget& independently_resolved);
std::wstring voltage_scaffold_receipt_path(const std::wstring& state_directory,const std::string& slot);
VoltageScaffoldReceipt read_voltage_scaffold_receipt(const std::wstring& state_directory,const dc_target& freshly_bound,const VoltageScaffoldWindowsTarget& independently_resolved);
}
