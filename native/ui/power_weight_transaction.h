// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "power_weight_receipt.h"
#include "power_weight_registry_write.h"
namespace druta::ui {
enum class PowerWeightOperation {apply,remove,verify};
struct PowerWeightBoundaryResult {
 bool ok=false,reboot_required=false,timed_out=false,cleanup_pending=false,uncertain=false;
 std::string message;
 bool retryable=false; // freshly opened clock driver is still initializing
 bool image_matches_timing_pending=false; // the image is correct but fresh physical P0 verification is unavailable
};
struct PowerWeightRegistryRecovery {VoltageScaffoldWindowsTarget windows;PowerWeightRegistrySnapshot registry;};
struct PowerWeightTransactionApi {
 std::function<PowerWeightCapture(dc_gpu*,bool)> capture;
 // Removal-only fallback when live capture is transient/unavailable. Must prove
 // saved PCI instance DEV/SUBSYS and current Enum Driver mapping independently.
 std::function<PowerWeightRegistryRecovery(const PowerWeightReceipt&)> recovery_registry;
 // create=true must durably publish the receipt AND matching active pointer.
 // Later saves must require that same active receipt. Throws on failure.
 std::function<void(const PowerWeightReceipt&,bool create)> persist;
 std::function<bool(const PowerWeightReceipt&)> clear_active;
 std::function<PowerWeightRegistryWriteResult(const VoltageScaffoldWindowsTarget&,const PowerWeightRegistrySnapshot&,const PowerWeightRegistrySnapshot&,const std::function<bool(const std::wstring&,std::string&)>&)> write;
 std::function<PowerWeightBoundaryResult(const VoltageScaffoldWindowsTarget&)> restart;
 // Must use a fresh process and independently verify receipt target, complete
 // expected/original image and INFO. A registry SET is never activation proof.
 std::function<PowerWeightBoundaryResult(const PowerWeightReceipt&,bool original)> fresh_verify;
 // Clock-only: observe the expected running image while the exact staged
 // records are still present. Each call opens an independent process.
 std::function<PowerWeightBoundaryResult(const PowerWeightReceipt&)> fresh_verify_staged;
 std::function<void()> wait_clock_ready;
};
struct PowerWeightTransactionResult {
 bool ok=false,no_change=false,may_have_changed=false,reboot_required=false,timed_out=false,cleanup_pending=false,recovery_required=false;
 std::string message;std::shared_ptr<const PowerWeightReceipt> receipt;
};
// Clock-only registry cleanup for a failed or interrupted helper. The caller
// holds the active receipt's operation lock. No live GPU API or restart is used.
PowerWeightTransactionResult recover_staged_clocks(PowerWeightTransactionApi&, PowerWeightReceipt);
bool power_weight_verify_registry(const PowerWeightReceipt&,const PowerWeightRegistrySnapshot& current,bool original,bool staged=false);
class PowerWeightTransaction {
public:
 PowerWeightTransaction(PowerWeightTransactionApi,dc_gpu*,const dc_gpu_identity&);
 // Explicit calls only. Removal also recovers partial/uncertain Apply. Verify
 // never writes registry or restarts; original selects removal verification.
 PowerWeightTransactionResult run(PowerWeightOperation,const PowerWeightReceipt&,bool original=false);
private:PowerWeightTransactionApi api_;dc_gpu* gpu_;dc_gpu_identity opened_;
};
}
