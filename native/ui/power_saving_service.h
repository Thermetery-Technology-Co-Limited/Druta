// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "voltage_policy_service.h"
#include "profile_io.h"
#include "saving_profile.h"
#include <functional>
#include <vector>
namespace druta::ui {
enum class PowerSavingFamily {gr_elpg,individual,idle_threshold,adaptive,difr};
struct PowerSavingKey {PowerSavingFamily family=PowerSavingFamily::gr_elpg;uint32_t index=0;};
bool operator==(const PowerSavingKey&,const PowerSavingKey&);
struct PowerSavingRow {
    PowerSavingKey key;std::string label,binding,capability,message;
    VoltagePolicyAvailability availability=VoltagePolicyAvailability::unknown;
    bool stale=false,supported=false,writable=false,owned=false,uncertain=false;
    // value/initial are this application's request, never an aggregate predicate.
    std::optional<uint32_t> value,initial;std::optional<bool> aggregate_enabled;
    uint32_t reason_a=0,reason_b=0,raw_first=0,raw_second=0,scaled_first=0,scaled_second=0;
    uint32_t scale=0,maximum_us=0;
};
struct PowerSavingData {
    uint64_t revision=0;dc_gpu_identity identity{};std::vector<PowerSavingRow> rows;
    std::optional<dc_target> target;
    bool undo_available=false,recovery_pending=false,owned_requests=false;
    std::string message;
};
struct PowerSavingRequest {
    PowerSavingKey key;uint32_t value=0; // Boolean: 1 hold-disable, 0 release; adaptive: 1 Allow, 0 Disable; idle: integer us.
    std::string binding;std::optional<uint32_t> expected;
};
struct PowerSavingResult {bool ok=false,may_have_changed=false,rollback_verified=false;std::string message;};
// Current-session semantic plans and issued-request journals, never packets
// loaded from disk. All selected fields (including no-ops) remain in the plan.
struct PowerSavingPlan {struct State;bool ok=false;std::string message;std::shared_ptr<const State> state;};
struct PowerSavingReceipt {struct State;std::shared_ptr<State> state;};
struct PowerSavingApi {
    VoltagePolicyApi transport;
    std::function<uint32_t()> profiler_handle; // optional fresh 0x60xxxxxx candidate, never borrowed
};
class PowerSavingService {
public:
    PowerSavingService(PowerSavingApi,dc_gpu*,const dc_gpu_identity&);
    ~PowerSavingService(); // no writes, persistence, or automatic replay
    void set_history_callback(std::function<void()>); // standalone explicit effects only
    const PowerSavingData& read(uint32_t families=31);
    PowerSavingResult apply(const PowerSavingRequest&);
    // Coupled profiles retain selected idle originals before adaptive effects.
    // GR/count composition supports attributable exclusive handovers; shared
    // simultaneous ownership without an independent count proof is refused.
    PowerSavingPlan preview(const std::vector<PowerSavingRequest>&);
    PowerSavingResult apply_profile(const PowerSavingPlan&,const std::function<void(const PowerSavingReceipt&)>& before_write);
    PowerSavingResult restore(const PowerSavingReceipt&);
    PowerSavingResult verify(const PowerSavingPlan&);
    PowerSavingResult verify(const PowerSavingReceipt&,bool original=false);
    PowerSavingResult undo();PowerSavingResult recover();
    PowerSavingResult restore_initial(const PowerSavingKey&);
    bool has_owned_requests() const;
    PowerSavingResult release_owned(); // explicit lifecycle, safe owned requests only
    const PowerSavingData& state() const;
private:struct Impl;std::unique_ptr<Impl> impl_;
};
ProfileJson capture_power_saving_profile(const PowerSavingData&,const std::vector<PowerSavingKey>&);
std::vector<PowerSavingRequest> stage_power_saving_profile(const ProfileJson&,const PowerSavingData&);
ProfileSelection describe_power_saving_profile(const ProfileJson&);
ProfileJson select_power_saving_profile(const ProfileJson&,const std::vector<std::string>&);
}
