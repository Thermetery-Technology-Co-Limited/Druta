// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <cstdint>
#include <functional>
#include <string>
#include <vector>
namespace druta::ui {
struct DeviceRestartTarget {std::string slot;std::wstring instance_id,name;};
struct DeviceRestartResult {bool ok=false,reboot_required=false,uncertain=false;std::string message;};
// Throws on malformed/unsupported segment, no matching target, or ambiguity.
DeviceRestartTarget resolve_restart_target(const std::string&,const std::vector<DeviceRestartTarget>&);
struct DeviceRestartApi {
 std::function<std::vector<DeviceRestartTarget>()> enumerate;
 std::function<DeviceRestartResult(const DeviceRestartTarget&,uint32_t)> launch_child;
 std::function<bool(const DeviceRestartTarget&,std::string&)> started;
};
DeviceRestartResult restart_device(const DeviceRestartTarget&,const DeviceRestartApi&,uint32_t timeout_ms=30000);
// Explicit helper mode only: re-resolves slot and instance before property change.
int device_restart_child(const std::string& slot,const std::wstring& instance_id);
std::vector<DeviceRestartTarget> enumerate_restart_targets();
DeviceRestartResult launch_device_restart_child(const std::wstring& executable,const DeviceRestartTarget&,uint32_t timeout_ms);
bool restart_target_started(const DeviceRestartTarget&,std::string& reason);
}
