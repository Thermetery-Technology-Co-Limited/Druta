// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <drutacore/drutacore.h>
#include <array>
#include <optional>
#include <string>
#include <vector>
namespace druta::ui {
struct PowerIntegralApi {
    decltype(&dc_gpu_lock) lock=nullptr;
    decltype(&dc_gpu_unlock) unlock=nullptr;
    decltype(&dc_gpu_target_bind) target_bind=nullptr;
    decltype(&dc_gpu_identity_get) identity=nullptr;
    decltype(&dc_perf_probe_architecture) probe_architecture=nullptr;
    decltype(&dc_cl_abi) abi=nullptr;
    decltype(&dc_cl_rm) rm=nullptr;
    decltype(&dc_rm_transport_reset) transport_reset=nullptr;
    decltype(&dc_cl_policy_mask_set) policy_mask_set=nullptr;
};
enum class PowerIntegralField { lower,upper };
struct PowerIntegralUndo {
    bool owned=false,confirmed=false,recovery=false,conflict=false;
    uint16_t initial=0,previous=0,requested=0;
    bool can_undo=false;
};
struct PowerIntegralRow {
    uint32_t policy=0,type=0,channel=0,unit=0,request=0;
    uint16_t lower=0,upper=0,reported_lower=0,reported_upper=0;
    uint8_t past_samples=0,next_samples=0;
    bool available=false;
    std::string reason;
    std::array<PowerIntegralUndo,2> undo{};
};
struct PowerIntegralData {
    bool current=false;
    std::optional<dc_target> target;
    std::vector<PowerIntegralRow> rows;
    std::string message;
};
struct PowerIntegralResult {
    bool ok=false,may_have_changed=false,rollback_attempted=false,rollback_verified=false;
    std::string message;
};
// Ratio is an unsigned U4.12 value, rounded half up, not a measured power limit.
std::optional<uint16_t> power_integral_percent_to_raw(double percent);
double power_integral_raw_to_percent(uint16_t raw);
// One worker-owned instance per open adapter. No destructor writes, persistence,
// opaque replay, automatic startup apply or implicit whole-table restoration.
class PowerIntegralService {
public:
    PowerIntegralService(PowerIntegralApi api,dc_gpu* gpu,const dc_gpu_identity& identity);
    PowerIntegralData read();
    PowerIntegralResult apply(uint32_t policy,PowerIntegralField field,uint32_t raw,std::optional<uint16_t> expected={});
    PowerIntegralResult restore(uint32_t policy,PowerIntegralField field);
    PowerIntegralResult undo(uint32_t policy,PowerIntegralField field);
    const PowerIntegralData& data() const {return data_;}
    bool has_undo() const;
private:
    struct Snapshot {dc_power_layout layout{};std::vector<uint32_t> info,control;};
    struct Owned {
        PowerIntegralUndo value;std::vector<uint32_t> info_row;bool pending=false;
        std::optional<uint16_t> pending_before,pending_after;
    };
    PowerIntegralApi api_;dc_gpu* gpu_;std::optional<dc_target> target_;
    dc_gpu_identity identity_{};
    PowerIntegralData data_;
    std::array<std::array<Owned,2>,32> owned_{};
    bool binding();
    bool capture(Snapshot& state);
    bool capture_once(Snapshot& state,int32_t& kind);
    void publish(const Snapshot& state);
    bool dispatch(const Snapshot& state,uint32_t policy,PowerIntegralField field,uint16_t raw,Snapshot& after);
    PowerIntegralResult restore_locked(uint32_t policy,PowerIntegralField field,bool previous);
};
}
