// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "voltage_evidence_provider.h"
#include "voltage_scaffold_receipt.h"
namespace druta::ui {
struct VoltageScaffoldRead {
 dc_target target{};std::vector<InstalledVoltageScaffoldHead> heads;
 std::string receipt_binding,image_sha256,message;
 std::shared_ptr<const VoltageEquationPackets> equations;
};
struct VoltageScaffoldHostApi {
 std::function<std::wstring()> state_directory;
 std::function<VoltageScaffoldWindowsTarget(const dc_gpu_identity&,const dc_target&)> resolve_windows;
 std::function<VoltageScaffoldReceipt(const std::wstring&,const dc_target&,const VoltageScaffoldWindowsTarget&)> load_receipt;
 std::function<VoltageEquationPackets(dc_gpu*)> capture_equations;
};
// Construction performs no I/O. Each explicit call independently reads and
// recognizes installation; captured values are inspected requests, not writers.
// Optional raw capture injection must not call a semantic provider. Production
// uses VoltageLimitsService::capture_equations with no semantic callback.
std::function<VoltageScaffoldRead(dc_gpu*)> make_voltage_scaffold_provider(
 VoltageEvidenceApi,const dc_gpu_identity&,VoltageScaffoldHostApi host={});
}
