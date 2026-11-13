// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "voltage_policy_service.h"
#include "profile_io.h"
#include "profile_selection.h"
#include <functional>
#include <vector>
namespace druta::ui {
enum class VoltageTableFamily { requests,offsets };
struct VoltageTableKey {VoltageTableFamily family=VoltageTableFamily::requests;uint32_t rail=0,field=0;};
bool operator==(const VoltageTableKey&,const VoltageTableKey&);
struct VoltageTableRow {
    VoltageTableKey key;
    std::string label,binding,message;
    VoltagePolicyAvailability availability=VoltagePolicyAvailability::unknown;
    bool stale=false,writable=false;
    std::optional<int32_t> value,initial;
    uint32_t policy_type=0,control_type=0;
};
struct VoltageTablesData {
    uint64_t revision=0;
    dc_gpu_identity identity{};
    std::optional<dc_target> target;
    std::vector<VoltageTableRow> rows;
    bool requests_complete=false,undo_available=false,recovery_pending=false;
    std::optional<bool> gate_enabled;
    std::optional<int32_t> boost_percent;
    std::string requests_message,offsets_message,message;
};
struct VoltageTableRequest {
    VoltageTableKey key;int64_t value=0;
    std::string binding;
    std::optional<int32_t> expected;
};
struct VoltageTablesResult {bool ok=false,may_have_changed=false,rollback_verified=false;std::string message;};
// Process-local selected-value contracts; no captured packet is replayed.
struct VoltageTablesPlan {struct State;bool ok=false;std::string message;std::shared_ptr<const State> state;};
struct VoltageTablesReceipt {struct State;std::shared_ptr<State> state;};
struct VoltageTablesApi {
    VoltagePolicyApi transport;
    decltype(&dc_rails_blocks) rails_blocks=nullptr;
    decltype(&dc_rails_state) rails_state=nullptr;
    decltype(&dc_rails_deltas) rails_deltas=nullptr;
    decltype(&dc_rails_refresh) rails_refresh=nullptr;
    decltype(&dc_gpu_voltage_boost_get) boost=nullptr;
    decltype(&dc_nvapi_interface_present) interface_present=nullptr;
    // Narrow host adapter for NvAPI 0x87C55C8A only, with V2 0xAC8 bytes.
    // DC_E_UNAVAILABLE guarantees no dispatch; every other result may follow
    // a SET. status is the original NvAPI result, not inferred success.
    std::function<dc_status(dc_gpu*,uint8_t*,size_t,int32_t&,dc_error&)> public_set;
};
// Worker-owned, opened-UUID bound. Complete request preservation is mandatory
// for the public six-word SET; partial inventories remain independently readable.
class VoltageTablesService {
public:
    VoltageTablesService(VoltageTablesApi,dc_gpu*,const dc_gpu_identity&);
    ~VoltageTablesService(); // no destructor writes, persistence or replay
    const VoltageTablesData& read(uint32_t families=3); // bit1 requests, bit2 scaffold offsets
    VoltageTablesResult apply(const VoltageTableRequest&);
    VoltageTablesResult apply(const std::vector<VoltageTableRequest>&);
    VoltageTablesPlan preview(const std::vector<VoltageTableRequest>&,std::optional<bool> projected_admission={});
    VoltageTablesResult apply_profile(const VoltageTablesPlan&,
        const std::function<void(const VoltageTablesReceipt&)>& before_write);
    VoltageTablesResult restore(const VoltageTablesReceipt&);
    VoltageTablesResult verify(const VoltageTablesPlan&);
    VoltageTablesResult verify(const VoltageTablesReceipt&,bool original=false);
    VoltageTablesResult undo();
    VoltageTablesResult recover();
    VoltageTablesResult restore_initial(const VoltageTableKey&);
    const VoltageTablesData& state() const;
private:struct Impl;std::unique_ptr<Impl> impl_;
};
// Typed values/identity only. These documents never contain packets to replay.
ProfileJson capture_voltage_tables_profile(const VoltageTablesData&,const std::vector<VoltageTableKey>&);
std::vector<VoltageTableRequest> stage_voltage_tables_profile(const ProfileJson&,const VoltageTablesData&);
ProfileSelection describe_voltage_tables_profile(const ProfileJson&);
ProfileJson select_voltage_tables_profile(const ProfileJson&,const std::vector<std::string>& targets);
}
