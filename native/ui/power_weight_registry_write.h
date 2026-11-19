// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "power_weight_registry_host.h"
#include <optional>
namespace druta::ui {
struct PowerWeightRegistryWriteResult {bool ok=false,may_have_changed=false;std::string message;};
struct PowerWeightRegistryWriteApi {
 PowerWeightRegistryReader read;
 // A false result is potentially dispatched; caller must retain its journal.
 std::function<bool(const std::wstring& key,const std::wstring& name,const std::optional<PowerWeightRegistryValue>& value,std::string& error)> write;
};
// Explicit selected delta only. before_effect must durably journal each name;
// false/throw stops before dispatch. No automatic rollback or refresh.
PowerWeightRegistryWriteResult write_power_weight_registry(const VoltageScaffoldWindowsTarget&,const PowerWeightRegistrySnapshot& expected,const PowerWeightRegistrySnapshot& desired,const std::function<bool(const std::wstring&,std::string&)>& before_effect,const PowerWeightRegistryWriteApi& api={});
}
