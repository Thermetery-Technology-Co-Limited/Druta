// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <drutacore/drutacore.h>
#include "profile_io.h"
#include <array>
#include <functional>
#include <memory>
#include <optional>
#include <string>
namespace druta::ui {
inline constexpr uint32_t voltage_admission=1,voltage_monotonicity=2,voltage_smoothing=4,voltage_vf_policies=6,voltage_all_policies=7;
enum class VoltagePolicyAvailability {unknown,available,transient,unsupported,invalid};
struct VoltagePolicyApi {
    decltype(&dc_gpu_identity_get) identity=nullptr;
    decltype(&dc_gpu_target_bind) bind=nullptr;
    decltype(&dc_perf_probe_architecture) architecture_supported=nullptr;
    decltype(&dc_perf_transport) transport=nullptr;
    decltype(&dc_rm_escape_raw) escape=nullptr;
    decltype(&dc_rm_transport_reset) reset_transport=nullptr;
    decltype(&dc_gpu_lock) lock=nullptr;
    decltype(&dc_gpu_unlock) unlock=nullptr;
};
struct VoltagePolicyRow {
    std::optional<bool> current,initial;
    VoltagePolicyAvailability availability=VoltagePolicyAvailability::unknown;
    bool writable=false;
    std::string message;
};
struct VoltagePolicyData {
    uint64_t revision=0;
    dc_gpu_identity identity{};
    std::optional<dc_target> target;
    std::array<VoltagePolicyRow,3> rows;
    bool undo_available=false,recovery_pending=false,recovery_prior_unknown=false;
    uint32_t undo_selection=0,recovery_selection=0;
    std::string message;
};
struct VoltagePolicyRequest {
    // Admission is its own transaction; VF may select one or both known flags.
    uint32_t selection=0,values=0;
    std::optional<uint32_t> expected;
};
struct VoltagePolicyResult {
    bool ok=false,may_have_changed=false,rollback_verified=false;
    std::string message;
};
// Process-local typed journal. Its private state contains selected Boolean
// requests and prior Undo, never an opaque firmware snapshot for replay.
struct VoltagePolicyReceipt {struct State;std::shared_ptr<State> state;int restoration_order() const;};
class VoltagePolicyService {
public:
    VoltagePolicyService(VoltagePolicyApi api,dc_gpu* gpu,const dc_gpu_identity& target);
    ~VoltagePolicyService(); // no destructor writes or persistent requests
    void set_history_callback(std::function<void()> callback);
    const VoltagePolicyData& read(uint32_t selection=voltage_all_policies);
    VoltagePolicyResult apply(const VoltagePolicyRequest& request);
    VoltagePolicyResult preview(const VoltagePolicyRequest& request);
    VoltagePolicyResult apply_profile(const VoltagePolicyRequest&,
        const std::function<void(const VoltagePolicyReceipt&)>& before_write);
    VoltagePolicyResult restore(const VoltagePolicyReceipt&);
    VoltagePolicyResult verify(const VoltagePolicyReceipt&,bool original=false);
    VoltagePolicyResult restore_initial(uint32_t selection);
    VoltagePolicyResult undo();
    VoltagePolicyResult recover();
    const VoltagePolicyData& state() const;
private:
    struct Impl;std::unique_ptr<Impl> impl_;
};
// Files contain semantic Boolean selections only. Admission and VF flags are
// separate transactions. Parsing/staging never reads hardware or applies.
struct VoltagePolicyProfile {
    std::string slot,uuid;
    uint32_t architecture=0,device_id=0,subsystem_id=0;
    VoltagePolicyRequest request;
};
ProfileJson capture_voltage_policy_profile(const VoltagePolicyData&,uint32_t selection);
VoltagePolicyProfile validate_voltage_policy_profile(const ProfileJson&);
VoltagePolicyRequest stage_voltage_policy_profile(const ProfileJson&,const VoltagePolicyData&);
}
