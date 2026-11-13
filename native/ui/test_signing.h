// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <functional>
#include <optional>
#include <string>
#include <vector>
namespace druta::ui {
struct TestSigningProcessResult {bool launched=false,timed_out=false;uint32_t exit_code=0;std::string output,error;};
struct TestSigningStatus {std::optional<bool> secure_boot;std::optional<bool> administrator;std::optional<std::string> bitlocker_protected;};
struct TestSigningCommand {std::wstring executable;std::vector<std::wstring> arguments;};
struct TestSigningCommandResult {TestSigningCommand command;TestSigningProcessResult process;bool ok=false;};
struct TestSigningResult {bool ok=false,refused=false,may_have_changed=false;TestSigningStatus status;std::vector<TestSigningCommandResult> commands;std::string message;};
struct TestSigningApi {
 std::function<TestSigningStatus()> probe;
 std::function<TestSigningProcessResult(const TestSigningCommand&,uint32_t timeout_ms)> run;
};
// Exact released recipes, provided for review/copy; constructing these is read-only.
std::vector<TestSigningCommand> test_signing_recipe(bool enable);
// Explicit read-only status or explicit mutation; never reboot automatically.
TestSigningStatus read_test_signing_status(const TestSigningApi& api={});
TestSigningResult execute_test_signing(bool enable,const TestSigningApi& api={});
}
