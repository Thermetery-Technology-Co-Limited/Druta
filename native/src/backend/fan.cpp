// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/fan.h"

#include <algorithm>

#include "core/sleep.h"

namespace druta::backend {
namespace {

using HandleU32Fn = int(DRUTA_CDECL*)(void*, std::uint32_t*);
using HandleIdxU32Fn = int(DRUTA_CDECL*)(void*, std::uint32_t, std::uint32_t*);
using SetFanFn = int(DRUTA_CDECL*)(void*, std::uint32_t, std::uint32_t);
using DefaultFanFn = int(DRUTA_CDECL*)(void*, std::uint32_t);
using CoolerLevelsSetFn = int(DRUTA_CDECL*)(void*, std::uint32_t, void*, std::uint32_t);
using CoolerRestoreFn = int(DRUTA_CDECL*)(void*, void*, std::uint32_t);
using HandlePtrFn = int(DRUTA_CDECL*)(void*, void*);

constexpr std::uint32_t CoolerLevelsSet = 0x891FA0AE;
constexpr std::uint32_t CoolerRestore = 0x8F6ED0FB;
constexpr std::uint32_t FanCoolersSetControl = 0xA58971A5;

struct CoolerLevel {
    std::uint32_t level, policy;
};
struct CoolerLevels {
    std::uint32_t version;
    CoolerLevel entries[20];
};
static_assert(sizeof(CoolerLevels) == 164);

struct Plan {
    std::uint32_t id;
    std::int32_t level;
    bool manual;
    std::optional<std::uint32_t> policy;
};

}  // namespace

std::optional<NativeFanState> FanControl::read_fan_control_state() {
    // A fan ramps after a request: snapshot its requested level and policy,
    // not the intermediate speed, so Undo restores what was requested.
    Nvml& nv = gpu_.nvml;
    if (nv.ok && nv.has("nvmlDeviceGetNumFans") && nv.has("nvmlDeviceGetFanControlPolicy_v2") &&
        nv.has("nvmlDeviceGetTargetFanSpeed")) {
        std::uint32_t count = 0;
        if (nv.get<HandleU32Fn>("nvmlDeviceGetNumFans")(nv.dev, &count) == 0 && 0 < count && count <= 32) {
            NativeFanState state;
            state.source = "nvml";
            auto policy_fn = nv.get<HandleIdxU32Fn>("nvmlDeviceGetFanControlPolicy_v2");
            auto target_fn = nv.get<HandleIdxU32Fn>("nvmlDeviceGetTargetFanSpeed");
            for (std::uint32_t index = 0; index < count; ++index) {
                std::uint32_t policy = 0, level = 0;
                if (policy_fn(nv.dev, index, &policy) != 0 || target_fn(nv.dev, index, &level) != 0 ||
                    (policy != 0 && policy != 1) || level > 100)
                    break;
                NativeFan fan;
                fan.id = index;
                fan.level = level;
                fan.policy = policy;
                fan.default_policy = 0;
                fan.manual = policy == 1;
                fan.min = fan_min();
                fan.max = fan_max();
                state.fans.push_back(fan);
            }
            if (state.fans.size() == count) return state;
        }
    }
    return telemetry_.native_fan_data();
}

std::optional<bool> FanControl::read_fan_manual() {
    // True/False when every fan shares that policy; nullopt for unknown/mixed.
    if (auto state = read_fan_control_state()) {
        bool first = state->fans.front().manual;
        for (const NativeFan& fan : state->fans)
            if (fan.manual != first) return std::nullopt;
        return first;
    }
    Nvml& nv = gpu_.nvml;
    if (auto fn = nv.ok ? nv.get<HandleIdxU32Fn>("nvmlDeviceGetFanControlPolicy_v2") : nullptr) {
        std::uint32_t policy = 0;
        if (fn(nv.dev, 0, &policy) == 0 && (policy == 0 || policy == 1)) return policy == 1;
    }
    return std::nullopt;
}

FanCapabilities FanControl::fan_capabilities() {
    Nvml& nv = gpu_.nvml;
    std::uint32_t count = 0;
    bool has_count = nv.ok && nv.has("nvmlDeviceGetNumFans") &&
                     nv.get<HandleU32Fn>("nvmlDeviceGetNumFans")(nv.dev, &count) == 0 && 0 < count && count <= 32;
    FanCapabilities result;
    result.manual = has_count && nv.has("nvmlDeviceSetFanSpeed_v2");
    result.automatic = has_count && nv.has("nvmlDeviceSetDefaultFanSpeed_v2");
    result.source = "nvml";
    result.min = fan_min();
    result.max = fan_max();
    if (auto native = telemetry_.native_fan_data()) {
        Nvapi& a = gpu_.nvapi;
        bool fallback_manual, fallback_auto;
        if (native->source == "nvapi_cooler") {
            fallback_manual = a.interface(CoolerLevelsSet) != nullptr;
            fallback_auto = a.interface(CoolerRestore) != nullptr;
        } else {
            fallback_manual = fallback_auto = a.interface(FanCoolersSetControl) != nullptr;
        }
        bool nvml_manual = result.manual, nvml_auto = result.automatic;
        result.manual = nvml_manual || fallback_manual;
        result.automatic = nvml_auto || fallback_auto;
        std::uint32_t lo = 0, hi = 100;
        bool first = true;
        for (const NativeFan& fan : native->fans) {
            lo = first ? fan.min : std::max(lo, fan.min);
            hi = first ? fan.max : std::min(hi, fan.max);
            first = false;
        }
        result.min = lo;
        result.max = hi;
        if (!(nvml_manual && nvml_auto)) result.source = native->source;
    }
    return result;
}

Outcome FanControl::restore_fan_control_state(const FanSnapshot& state) {
    auto current = read_fan_control_state();
    if (!current) return {false, "fan control state unavailable"};
    Nvml& nv = gpu_.nvml;
    Nvapi& a = gpu_.nvapi;
    // A partially exposed modern API must not hide a complete native fallback.
    if (current->source == "nvml") {
        bool missing = false;
        for (const FanSnapshotRow& row : state.fans)
            if (row.has_manual && !nv.has(row.manual ? "nvmlDeviceSetFanSpeed_v2" : "nvmlDeviceSetDefaultFanSpeed_v2"))
                missing = true;
        if (missing) {
            if (auto native = telemetry_.native_fan_data()) current = native;
        }
    }
    const std::vector<NativeFan>& rows = current->fans;
    if (state.fans.size() != rows.size() || rows.empty())
        return {false, "fan count changed; refusing to restore another layout"};
    if (state.source != "nvml" && state.source != "nvapi_cooler" && state.source != "nvapi_client")
        return {false, "fan snapshot has an unknown control source"};
    const bool same_source = state.source == current->source;
    std::vector<Plan> plan;
    for (std::size_t i = 0; i < rows.size(); ++i) {
        const FanSnapshotRow& old = state.fans[i];
        const NativeFan& now = rows[i];
        if (!old.has_manual) return {false, "fan snapshot has an unknown policy"};
        if (same_source && (!old.has_id || old.id != now.id))
            return {false, "fan IDs changed; refusing to restore another layout"};
        if (!old.level_valid) return {false, "fan snapshot has an invalid requested level"};
        if (old.manual && !(static_cast<std::int32_t>(now.min) <= old.level && old.level <= static_cast<std::int32_t>(now.max)))
            return {false, "fan " + std::to_string(now.id) + " request outside [" + std::to_string(now.min) + ".." +
                               std::to_string(now.max) + "]%"};
        std::optional<std::uint32_t> policy;
        if (same_source) {
            if (old.has_policy) policy = old.policy;
        } else {
            policy = old.manual ? 1u : now.default_policy;
        }
        if (!old.manual && (!policy || *policy != now.default_policy))
            return {false, "non-default automatic fan policy cannot be restored safely"};
        plan.push_back({now.id, old.level, old.manual, policy});
    }
    std::vector<std::string> errors;
    if (current->source == "nvml") {
        for (const Plan& p : plan) {
            const char* name = p.manual ? "nvmlDeviceSetFanSpeed_v2" : "nvmlDeviceSetDefaultFanSpeed_v2";
            if (!nv.has(name)) return {false, std::string(name) + " not available"};
        }
        for (const Plan& p : plan) {
            int st = p.manual ? nv.get<SetFanFn>("nvmlDeviceSetFanSpeed_v2")(nv.dev, p.id, static_cast<std::uint32_t>(p.level))
                              : nv.get<DefaultFanFn>("nvmlDeviceSetDefaultFanSpeed_v2")(nv.dev, p.id);
            if (st != 0) errors.push_back("fan" + std::to_string(p.id) + ":" + nv.errstr(st));
        }
    } else if (current->source == "nvapi_cooler") {
        for (const Plan& p : plan)
            if (!a.interface(p.manual ? CoolerLevelsSet : CoolerRestore)) return {false, "NVAPI cooler controls unavailable"};
        for (const Plan& p : plan) {
            int st;
            if (p.manual) {
                CoolerLevels buf{};
                buf.version = nvapi_ver<CoolerLevels>(1);
                buf.entries[0].level = static_cast<std::uint32_t>(p.level);
                buf.entries[0].policy = 1;
                st = a.get<CoolerLevelsSetFn>(CoolerLevelsSet)(a.gpu, p.id, &buf, 1);
            } else {
                std::uint32_t indexes[1] = {p.id};
                st = a.get<CoolerRestoreFn>(CoolerRestore)(a.gpu, indexes, 1);
            }
            if (st != 0) errors.push_back("fan" + std::to_string(p.id) + ":NVAPI status " + std::to_string(st));
        }
    } else {
        if (!a.interface(FanCoolersSetControl)) return {false, "NVAPI client fan controls unavailable"};
        auto native = telemetry_.native_fan_data();
        bool same_ids = native && native->source == "nvapi_client" && native->fans.size() == rows.size();
        for (std::size_t i = 0; same_ids && i < rows.size(); ++i) same_ids = native->fans[i].id == rows[i].id;
        if (!same_ids) return {false, "fan controls changed during preparation"};
        FanCoolersControl buf = native->control;  // the getter's own buffer: all IDs and reserved fields kept
        for (std::size_t index = 0; index < plan.size(); ++index) {
            buf.entries[index].level = static_cast<std::uint32_t>(plan[index].level);
            buf.entries[index].mode = plan[index].manual ? 1 : 0;
        }
        int st = a.get<HandlePtrFn>(FanCoolersSetControl)(a.gpu, &buf);
        if (st != 0) errors.push_back("NVAPI client fan status " + std::to_string(st));
    }
    if (!errors.empty()) {
        std::string joined;
        for (const std::string& e : errors) joined += (joined.empty() ? "" : "; ") + e;
        return {false, joined};
    }
    // Automatic requested levels can update asynchronously with temperature;
    // manual requests and every policy must agree once the driver settles
    // (GTX 690 / R472: about one second). Allow two seconds.
    for (int attempt = 0; attempt < 21; ++attempt) {
        std::optional<NativeFanState> after =
            current->source == "nvml" ? read_fan_control_state() : telemetry_.native_fan_data();
        bool agrees = after && after->source == current->source && after->fans.size() == plan.size();
        for (std::size_t i = 0; agrees && i < plan.size(); ++i) {
            const NativeFan& row = after->fans[i];
            const Plan& p = plan[i];
            agrees = row.id == p.id && row.manual == p.manual &&
                     (p.manual ? row.level == static_cast<std::uint32_t>(p.level) : (p.policy && row.policy == *p.policy));
        }
        if (agrees) return {true, "fan control state restored"};
        if (attempt < 20) sleep_ms(100);
    }
    return {false, "fan write accepted but requested policy/level did not read back"};
}

Outcome FanControl::set_nvapi_fans(std::optional<std::int32_t> percent) {
    auto native = telemetry_.native_fan_data();
    if (!native) return {false, "NVAPI fan controls not available"};
    FanSnapshot snapshot;
    snapshot.source = native->source;
    for (const NativeFan& fan : native->fans) {
        FanSnapshotRow row;
        row.has_manual = true;
        row.manual = percent.has_value();
        row.has_id = true;
        row.id = fan.id;
        row.has_policy = true;
        row.policy = percent ? 1u : fan.default_policy;
        row.level_valid = true;
        row.level = percent ? *percent : (native->source == "nvapi_client" ? 0 : static_cast<std::int32_t>(fan.level));
        snapshot.fans.push_back(row);
    }
    Outcome outcome = restore_fan_control_state(snapshot);
    if (!outcome.ok) return outcome;
    return {true, percent ? "fans set to manual " + std::to_string(*percent) + "%" : "fans returned to automatic"};
}

Outcome FanControl::set_fan(std::int32_t pct) {
    Nvml& nv = gpu_.nvml;
    if (!nv.ok || !nv.has("nvmlDeviceSetFanSpeed_v2") || !nv.has("nvmlDeviceGetNumFans")) return set_nvapi_fans(pct);
    const std::int32_t floor = static_cast<std::int32_t>(fan_min());
    if (pct < floor)
        return {false, "fan " + std::to_string(pct) + "% below hardware minimum " + std::to_string(floor) +
                           "% - use Auto for the zero-RPM idle curve"};
    pct = std::max(0, std::min(100, pct));
    std::uint32_t nf = 0;
    if (nv.get<HandleU32Fn>("nvmlDeviceGetNumFans")(nv.dev, &nf) != 0) {
        if (telemetry_.native_fan_data()) return set_nvapi_fans(pct);
        return {false, "fan count unavailable; cannot select fans to control"};
    }
    if (!nf) return {false, "no controllable fans reported"};
    std::string errs;
    for (std::uint32_t f = 0; f < nf; ++f) {
        int st = nv.get<SetFanFn>("nvmlDeviceSetFanSpeed_v2")(nv.dev, f, static_cast<std::uint32_t>(pct));
        if (st != 0) errs += (errs.empty() ? "" : "; ") + ("fan" + std::to_string(f) + ":" + nv.errstr(st));
    }
    if (errs.empty()) return {true, "fans set to manual " + std::to_string(pct) + "%"};
    return {false, errs + " (needs admin)"};
}

Outcome FanControl::reset_fan() {
    Nvml& nv = gpu_.nvml;
    if (!nv.ok || !nv.has("nvmlDeviceSetDefaultFanSpeed_v2") || !nv.has("nvmlDeviceGetNumFans"))
        return set_nvapi_fans(std::nullopt);
    std::uint32_t nf = 0;
    if (nv.get<HandleU32Fn>("nvmlDeviceGetNumFans")(nv.dev, &nf) != 0) {
        if (telemetry_.native_fan_data()) return set_nvapi_fans(std::nullopt);
        return {false, "fan count unavailable; cannot select fans to control"};
    }
    if (!nf) return {false, "no controllable fans reported"};
    std::string errs;
    for (std::uint32_t f = 0; f < nf; ++f) {
        int st = nv.get<DefaultFanFn>("nvmlDeviceSetDefaultFanSpeed_v2")(nv.dev, f);
        if (st != 0) errs += (errs.empty() ? "" : "; ") + ("fan" + std::to_string(f) + ":" + nv.errstr(st));
    }
    if (errs.empty()) return {true, "fans returned to automatic"};
    return {false, errs};
}

}  // namespace druta::backend
