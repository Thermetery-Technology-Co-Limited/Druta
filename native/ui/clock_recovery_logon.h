// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "power_weight_transaction.h"
namespace druta::ui {
namespace clock_logon_test {
void check_system_directory_permissions();
// OS interactions for exercising sign-in without starting Safe Mode or UAC.
struct SignIn {
 std::function<std::wstring()> executable;
 bool safe_mode=false, elevated=false;
 std::function<int(const std::wstring&,unsigned)> prompt;
 std::function<bool(const std::wstring&,const std::wstring&)> elevate;
};
int run(const SignIn&,const std::string& id,
        const std::function<PowerWeightTransactionResult(const PowerWeightReceipt&)>& recover);
}
bool arm_clock_logon_recovery(const PowerWeightReceipt&,std::string& error);
bool disarm_clock_logon_recovery(const PowerWeightReceipt&,std::string& error);
// Protected receipts for this Windows user, including consumed RunOnce entries.
std::vector<PowerWeightReceipt> clock_logon_recovery_enrollments();
bool clock_logon_recovery_enrolled(const PowerWeightReceipt&);
bool clock_logon_recovery_armed(const PowerWeightReceipt&);
bool clock_logon_receipts_match(const PowerWeightReceipt& pinned,const PowerWeightReceipt& current);
int run_clock_logon_recovery(const std::string& id,
    const std::function<PowerWeightTransactionResult(const PowerWeightReceipt&)>& recover);
}
