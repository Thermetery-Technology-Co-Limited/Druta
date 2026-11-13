// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "voltage_policy_service.h"
#include "profile_io.h"
#include <functional>
#include <vector>
namespace druta::ui {
enum class ClockGatingFamily {engine,block,second_level,pcie};
enum class ClockGatingMode {released,disabled,enabled};
struct ClockGatingRegister {
    uint32_t address=0,value=0,status=0,disabled_value=0,release_value=0;
    bool available=false;std::string qualification;
};
struct ClockGatingState {
    VoltagePolicyAvailability availability=VoltagePolicyAvailability::unknown;
    bool stale=false,support=false,owned=false,uncertain=false,arbitration_verified=false;
    bool can_disable=false,can_enable=false,can_release=false;
    ClockGatingMode mode=ClockGatingMode::released;
    std::string label,binding,layout,message,verification_scope;
    std::array<uint32_t,4> native{};
    std::optional<uint64_t> supported_engines,eligible_engines;
    uint64_t observed_engines=0;
    std::vector<ClockGatingRegister> registers;
    std::vector<uint32_t> unverified_source_peers;
    std::optional<uint32_t> target_generation,generation,root_generation,width,root_width,gpu_max,root_max,initial_generation;
    uint32_t target_register=0;std::vector<uint32_t> allowed_generations;
};
struct ClockGatingData {
    uint64_t revision=0;dc_gpu_identity identity{};
    std::array<ClockGatingState,4> families;
    bool needs_cleanup=false,undo_available=false;
    std::string message;
};
struct ClockGatingRequest {
    ClockGatingFamily family=ClockGatingFamily::engine;ClockGatingMode mode=ClockGatingMode::released;
    uint32_t generation=0;std::string binding;
    std::optional<uint32_t> expected_generation;
};
struct ClockGatingResult {bool ok=false,may_have_changed=false;std::string message;};
struct ClockGatingApi {
    VoltagePolicyApi transport;
    // Optional deterministic test seams. Host production can omit them.
    std::function<uint32_t()> next_object;
    std::function<void(uint32_t milliseconds)> sleep;
};
class ClockGatingService {
public:
    ClockGatingService(ClockGatingApi,dc_gpu*,const dc_gpu_identity&);
    ~ClockGatingService(); // host calls close before freeing GPU; no destructor replay
    const ClockGatingData& read(uint32_t families=15);
    ClockGatingResult apply(const ClockGatingRequest&);
    ClockGatingResult apply(const std::vector<ClockGatingRequest>&);
    ClockGatingResult release(ClockGatingFamily);
    ClockGatingResult restore_initial(ClockGatingFamily);
    ClockGatingResult recover(ClockGatingFamily);
    ClockGatingResult undo();
    ClockGatingResult close(); // release/free only owned profiler objects, never PCIe target replay
    bool needs_cleanup()const;
    const ClockGatingData& state()const;
private:struct Impl;std::unique_ptr<Impl> impl_;
};
ProfileJson capture_clock_gating_profile(const ClockGatingData&,const std::vector<ClockGatingFamily>&);
std::vector<ClockGatingRequest> stage_clock_gating_profile(const ProfileJson&,const ClockGatingData&);
}
