// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "device_restart.h"
#include <functional>
#include <string>
namespace druta::ui {
struct DeviceRecoveryRequest {
 DeviceRestartTarget target;std::string uuid;uint32_t parent_pid=0;
 std::wstring executable,core,receipt;
};
struct DeviceRecoveryReceipt {std::string slot,uuid,message;std::wstring instance_id;bool ok=false,reboot_required=false,uncertain=false;};
struct DeviceRecoveryApi {
 std::function<bool(const DeviceRecoveryRequest&,const DeviceRecoveryReceipt&,std::string&)> write_receipt;
 std::function<bool(uint32_t,uint32_t,std::string&)> wait_parent;
 std::function<DeviceRestartResult(const DeviceRestartTarget&)> restart;
 std::function<bool(const DeviceRecoveryRequest&,std::string&)> relaunch;
};
struct DeviceRecoveryResult {bool completed=false,parent_exited=false,relaunched=false;std::string message;};
// No restart or relaunch until parent exit confirmed. Receipt is written before
// waiting and again before relaunch. Callbacks never restore tuning.
DeviceRecoveryResult run_device_recovery(const DeviceRecoveryRequest&,const DeviceRecoveryApi&);
// Each path is absolute; receipts must be direct .json children of base.
bool valid_device_recovery_request(const DeviceRecoveryRequest&,const std::wstring& recovery_directory,std::string& reason);
// Uses LOCALAPPDATA only; empty means no verified local recovery directory.
std::wstring device_recovery_directory();
DeviceRecoveryReceipt read_device_recovery_receipt(const std::wstring& path,const std::wstring& base,const std::string& slot,const std::string& uuid);
std::wstring create_device_recovery_receipt(const std::wstring& recovery_directory);
// Builds only the selected GPU/core/receipt launch arguments; never tuning flags.
std::wstring device_recovery_relaunch_command(const DeviceRecoveryRequest&);
bool launch_device_recovery_helper(const DeviceRecoveryRequest&,std::string& reason);
int device_recovery_helper(const DeviceRecoveryRequest&,const std::wstring& recovery_directory);
}
