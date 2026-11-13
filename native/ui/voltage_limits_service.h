// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "voltage_policy_service.h"
#include "profile_io.h"
#include "profile_selection.h"
#include <functional>
#include <stdexcept>
#include <vector>
namespace druta::ui {
enum class VoltageLimitKind {rel,alt,ov,vmin,switch_delay};
struct VoltageLimitKey {VoltageLimitKind kind=VoltageLimitKind::rel;uint32_t rail=0,index=0;};
bool operator==(const VoltageLimitKey&,const VoltageLimitKey&);
struct VoltageEquationPackets {dc_target target{};std::vector<uint8_t> info,control;};
struct VoltageEquationRoot {uint32_t rail=0,equation=0;bool alt=false;std::vector<uint32_t> dependencies;};
struct VoltageEquationRole {
    VoltageLimitKey key;uint32_t next=0xFFFF,variable=0;
    bool graph_complete=false,shared=false,affected_clock_roots=false;
    // Exact ROM-derived immutable loaded row projection, with only value
    // float words excluded by this service. Never a packet to replay.
    std::vector<uint8_t> info_row,control_identity;
    int64_t stored_lower=0,stored_value=0;
    std::vector<VoltageEquationRoot> roots;
    VoltagePolicyAvailability availability=VoltagePolicyAvailability::available;
    std::string message;
};
struct VoltageEquationRail {
    bool present=false;
    VoltagePolicyAvailability availability=VoltagePolicyAvailability::unsupported;
    std::string message;
    // REL, ALT, OV, VMIN, effective; boost is independently reported.
    std::array<int64_t,5> policy{};std::array<int64_t,4> offsets{};int64_t boost_uv=0;
};
struct VoltageEquationEvidence {
    dc_target target{};std::string image_sha256;
    std::vector<VoltageEquationRole> roles;
    std::array<VoltageEquationRail,2> rails;
    std::optional<int32_t> boost_percent;
    // Opaque read-only comparison receipts, never written/restored. Provider
    // must include retained unknown fields and distinguish absent from failed
    // getters; a transient exception leaves established rows stale.
    std::vector<uint8_t> policy_identity,rail_requests,clock_requests,computed_curve;
    bool retained_requests_qualified=false;
};
struct VoltageEquationEvidenceError:std::runtime_error {
    VoltagePolicyAvailability kind;
    VoltageEquationEvidenceError(const std::string& message,VoltagePolicyAvailability value)
        :std::runtime_error(message),kind(value){}
};
struct VoltageLimitsApi {
    VoltagePolicyApi transport;
    decltype(&dc_nvapi_interface_present) interface_present=nullptr;
    // Fresh ROM role/dependency inspection plus independent policy and retained
    // request receipts. Production binds this per opened session; injected
    // clients may provide the same evidence contract without a driver.
    std::function<VoltageEquationEvidence(dc_gpu*,const VoltageEquationPackets&)> equation_evidence;
};
struct VoltageLimitRow {
    VoltageLimitKey key;std::string label,binding,message;
    VoltagePolicyAvailability availability=VoltagePolicyAvailability::unknown;
    bool writable=false,stale=false;
    std::optional<int64_t> lower,value,initial_lower,initial_value,stored_lower,stored_value,reported;
    uint32_t voltage_domain=0,operation_type=0,i2c_device_index=0;
};
struct VoltageLimitsData {
    uint64_t revision=0;dc_gpu_identity identity{};std::vector<VoltageLimitRow> rows;
    std::optional<dc_target> target;
    uint32_t equation_count=0,device_count=0;bool definitions_stale=false;
    bool undo_available=false,recovery_pending=false;
    std::string message,equations_message,devices_message;
    struct Definition {uint32_t index=0,type=0,next=0,variable=0;std::optional<int64_t> lower,value;std::string message;};
    std::vector<Definition> definitions; // read-only ordinals; no inferred ROM role
};
struct VoltageLimitRequest {
    VoltageLimitKey key;int64_t value=0;std::optional<int64_t> lower;
    std::string binding;std::optional<int64_t> expected_value,expected_lower;
};
struct VoltageLimitsResult {bool ok=false,may_have_changed=false,rollback_verified=false;std::string message;};
struct VoltageLimitsPlan {struct State;bool ok=false;std::string message;std::shared_ptr<const State> state;};
struct VoltageLimitsReceipt {struct State;std::shared_ptr<State> state;};
class VoltageLimitsService {
public:
    VoltageLimitsService(VoltageLimitsApi,dc_gpu*,const dc_gpu_identity&);
    ~VoltageLimitsService(); // no destructor writes
    // bit1: equations/roles; bit2: switch-delay devices. No automatic polling.
    const VoltageLimitsData& read(uint32_t families=3);
    // Read-only shared evidence capture. No semantic provider calls or state
    // publication; throws VoltageEquationEvidenceError on an unqualified read.
    VoltageEquationPackets capture_equations();
    VoltageLimitsResult apply(const std::vector<VoltageLimitRequest>&);
    VoltageLimitsPlan preview(const std::vector<VoltageLimitRequest>&);
    VoltageLimitsResult apply_profile(const VoltageLimitsPlan&,const std::function<void(const VoltageLimitsReceipt&)>& before_write);
    VoltageLimitsResult restore(const VoltageLimitsReceipt&);
    VoltageLimitsResult verify(const VoltageLimitsPlan&);
    VoltageLimitsResult verify(const VoltageLimitsReceipt&,bool original=false);
    VoltageLimitsResult apply(const VoltageLimitRequest& request){return apply(std::vector<VoltageLimitRequest>{request});}
    VoltageLimitsResult restore_initial(const VoltageLimitKey&);
    VoltageLimitsResult restore_rom(const VoltageLimitKey&);
    VoltageLimitsResult undo();VoltageLimitsResult recover();
    const VoltageLimitsData& state() const;
private:struct Impl;std::unique_ptr<Impl> impl_;
};
ProfileJson capture_voltage_limits_profile(const VoltageLimitsData&,const std::vector<VoltageLimitKey>&);
std::vector<VoltageLimitRequest> stage_voltage_limits_profile(const ProfileJson&,const VoltageLimitsData&);
ProfileSelection describe_voltage_limits_profile(const ProfileJson&);
ProfileJson select_voltage_limits_profile(const ProfileJson&,const std::vector<std::string>& targets);
}
