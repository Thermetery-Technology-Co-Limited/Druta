// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "power_weight_transaction.h"
#include "device_restart.h"
namespace druta::ui {
struct PowerWeightHostRequest {std::string receipt_id;PowerWeightOperation operation=PowerWeightOperation::verify;uint32_t parent_pid=0;std::wstring executable,core;};
// Testable admission and child-result checks used by the production host.
bool power_weight_host_gate(uint32_t,const std::function<bool(uint32_t)>& wait_parent,const std::function<void()>& admitted);
PowerWeightBoundaryResult power_weight_child_timeout(bool exit_confirmed);
PowerWeightBoundaryResult power_weight_restart_result(const DeviceRestartResult&);
bool power_weight_verify_registry(const PowerWeightReceipt&,const PowerWeightRegistrySnapshot& current,bool original);
bool power_weight_verify_result(const ProfileJson&,const std::string& receipt_id,bool original);
// Starts an owned helper; it waits for the initiating process to exit.
bool launch_power_weight_host(const PowerWeightHostRequest&,std::string& error);
// Call early in wmain. Returns true for a recognized helper mode.
bool dispatch_power_weight_host(int argc,wchar_t** argv,int& exit_code);
}
