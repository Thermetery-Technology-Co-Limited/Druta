// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "voltage_scaffold_provider.h"
namespace druta::ui {
struct VoltageOffsetKey {uint32_t rail=0;VbiosVoltageRole role=VbiosVoltageRole::rel;};
bool operator==(const VoltageOffsetKey&,const VoltageOffsetKey&);
struct VoltageOffsetRequest {VoltageOffsetKey key;int64_t value_uv=0;std::string binding;std::optional<int64_t> expected_uv;};
struct VoltageOffsetSnapshot {VoltageScaffoldRead scaffold;VoltageEquationPackets equations;VoltageEquationEvidence retained;};
struct VoltageOffsetsApi {VoltageLimitsApi transport;std::function<VoltageOffsetSnapshot(dc_gpu*)> capture;};
struct VoltageOffsetRow {
 VoltageOffsetKey key;std::string binding,message;
 VoltagePolicyAvailability availability=VoltagePolicyAvailability::unknown;bool writable=false,stale=false;
 std::optional<int64_t> value_uv,initial_uv,reported_uv;
};
struct VoltageOffsetsData {
 uint64_t revision=0;dc_gpu_identity identity{};std::optional<dc_target> target;
 std::vector<VoltageOffsetRow> rows;bool undo_available=false,recovery_pending=false;std::string message;
};
struct VoltageOffsetsResult {bool ok=false,may_have_changed=false,rollback_verified=false;std::string message;};
struct VoltageOffsetsPlan {struct State;bool ok=false;std::string message;std::shared_ptr<const State> state;};
struct VoltageOffsetsReceipt {struct State;std::shared_ptr<State> state;};
class VoltageOffsetsService {
public:
 VoltageOffsetsService(VoltageOffsetsApi,dc_gpu*,const dc_gpu_identity&);
 ~VoltageOffsetsService(); // Never restores or writes from destruction.
 const VoltageOffsetsData& read();const VoltageOffsetsData& state()const;
 VoltageOffsetsResult apply(const std::vector<VoltageOffsetRequest>&);
 VoltageOffsetsResult apply(const VoltageOffsetRequest& r){return apply(std::vector<VoltageOffsetRequest>{r});}
 VoltageOffsetsPlan preview(const std::vector<VoltageOffsetRequest>&);
 VoltageOffsetsResult apply_profile(const VoltageOffsetsPlan&,const std::function<void(const VoltageOffsetsReceipt&)>& before_write={});
 VoltageOffsetsResult verify(const VoltageOffsetsPlan&);
 VoltageOffsetsResult verify(const VoltageOffsetsReceipt&,bool original=false);
 VoltageOffsetsResult restore(const VoltageOffsetsReceipt&);
 VoltageOffsetsResult undo();VoltageOffsetsResult recover();VoltageOffsetsResult restore_initial(const VoltageOffsetKey&);
private:struct Impl;std::unique_ptr<Impl> impl_;
};
}
