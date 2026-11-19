/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "curve_profile.h"
#include <algorithm>
#include <cmath>
#include <limits>
#include <set>
#include <stdexcept>

namespace druta::ui {
namespace {
[[noreturn]] void fail(const char* why) { throw std::runtime_error(why); }
void keys(const ProfileJson& j, std::initializer_list<const char*> names) {
    if (!j.is_object() || j.size() != names.size()) fail("Invalid curve profile fields");
    for (const char* name : names) if (!j.contains(name)) fail("Missing curve profile field");
}
int64_t integer(const ProfileJson& j) {
    if (j.is_number_unsigned()) {
        const auto v=j.get<uint64_t>();
        if (v>static_cast<uint64_t>(INT64_MAX)) fail("Curve profile integer out of range");
        return static_cast<int64_t>(v);
    }
    if (!j.is_number_integer()) fail("Curve profile integer required");
    return j.get<int64_t>();
}
uint32_t uint32(const ProfileJson& j) {
    const auto v=integer(j); if (v<0 || v>UINT32_MAX) fail("Curve profile index out of range");
    return static_cast<uint32_t>(v);
}
double voltage(const ProfileJson& j) {
    if (!j.is_number()) fail("Curve profile voltage must be numeric");
    const double v=j.get<double>(); if (!std::isfinite(v) || v<=0) fail("Invalid curve voltage"); return v;
}
template<size_t N> std::string identity(const char (&value)[N]) {
    const auto end=std::find(value,value+N,'\0');
    if (end==value || end==value+N) fail("Curve adapter identity unavailable");
    return std::string(value,end);
}
void validate_current(const LiveData& d) {
    if (!d.connected || d.curve_max_abs_delta_khz<=0 || d.curve_max_abs_delta_khz>INT32_MAX)
        fail("Current curve or delta envelope unavailable");
    (void)identity(d.identity.uuid); (void)identity(d.identity.slot);
    const auto& l=d.curve_layout;
    if (l.size!=sizeof(l) || l.error[0] || !l.n_entries || l.n_entries>DC_VFP_POINTS ||
        !l.gpu_count || l.gpu_count>DC_VFP_POINTS || l.other_count>DC_VFP_POINTS ||
        l.gpu_count+l.other_count>l.n_entries || !l.freq_div || l.freq_div!=d.curve_freq_div ||
        d.curve.size()!=l.gpu_count) fail("Current curve layout unavailable or inconsistent");
    std::set<uint32_t> all, gpu;
    for (uint32_t i=0;i<l.gpu_count;++i) {
        if (l.gpu_idx[i]>=l.n_entries || !all.insert(l.gpu_idx[i]).second) fail("Invalid GPU curve layout");
        gpu.insert(l.gpu_idx[i]);
    }
    for (uint32_t i=0;i<l.other_count;++i)
        if (l.other_idx[i]>=l.n_entries || !all.insert(l.other_idx[i]).second) fail("Invalid non-GPU curve layout");
    std::set<uint32_t> observed;
    for (const auto& p:d.curve) {
        if (!gpu.count(p.idx) || !observed.insert(p.idx).second || !std::isfinite(p.volt_mv) || p.volt_mv<=0 ||
            !std::isfinite(p.freq_mhz) || p.freq_mhz<=0 || std::abs(static_cast<int64_t>(p.delta_khz))>d.curve_max_abs_delta_khz)
            fail("Current curve points unavailable or invalid");
    }
}
ProfileJson indices(const uint32_t* p, uint32_t count) {
    ProfileJson result=ProfileJson::array(); for (uint32_t i=0;i<count;++i) result.push_back(p[i]); return result;
}
bool same_curve(const std::vector<dc_vf_point>& a, const std::vector<dc_vf_point>& b) {
    if (a.size()!=b.size()) return false;
    for (const auto& p:a) {
        const auto q=std::find_if(b.begin(),b.end(),[&](const auto& x){return x.idx==p.idx;});
        if (q==b.end() || p.volt_mv!=q->volt_mv || p.freq_mhz!=q->freq_mhz || p.delta_khz!=q->delta_khz) return false;
    }
    return true;
}
}
ProfileJson capture_curve_profile(const LiveData& d) {
    validate_current(d); const auto& l=d.curve_layout;
    ProfileJson points=ProfileJson::array();
    for (const auto& p:d.curve) points.push_back({{"idx",p.idx},{"volt_mv",p.volt_mv},{"delta_khz",p.delta_khz}});
    return {{"format","druta-native-curve"},{"version",1},
        {"target",{{"uuid",identity(d.identity.uuid)},{"slot",identity(d.identity.slot)}}},
        {"geometry",{{"n_entries",l.n_entries},{"freq_div",l.freq_div},
            {"gpu_indices",indices(l.gpu_idx,l.gpu_count)},{"other_indices",indices(l.other_idx,l.other_count)}}},
        {"points",std::move(points)}};
}
void stage_curve_profile(const ProfileJson& profile,const SessionSnapshot& snapshot,CurveDraft& draft) {
    if (snapshot.busy || snapshot.opening || snapshot.curve_stale || !snapshot.curve_revision)
        fail("Fresh idle curve required");
    const auto& d=snapshot.data; validate_current(d);
    if (snapshot.selected_slot!=identity(d.identity.slot) || draft.generation()!=snapshot.generation ||
        draft.freq_div()!=d.curve_freq_div || draft.dirty() || !same_curve(draft.baseline(),d.curve))
        fail("Curve draft does not match the current clean baseline");
    CurveDraft staged=draft;
    if (!staged.revert() || !same_curve(staged.baseline(),d.curve)) fail("Fresh curve draft required");
    keys(profile,{"format","version","target","geometry","points"});
    if (!profile["format"].is_string() || profile["format"]!="druta-native-curve" || integer(profile["version"])!=1)
        fail("Unsupported curve profile format or version");
    const auto& target=profile["target"]; keys(target,{"uuid","slot"});
    if (!target["uuid"].is_string() || !target["slot"].is_string() ||
        target["uuid"].get<std::string>()!=identity(d.identity.uuid) || target["slot"].get<std::string>()!=identity(d.identity.slot))
        fail("Curve profile targets a different adapter");
    const auto& g=profile["geometry"]; keys(g,{"n_entries","freq_div","gpu_indices","other_indices"});
    const auto& l=d.curve_layout;
    if (uint32(g["n_entries"])!=l.n_entries || uint32(g["freq_div"])!=l.freq_div) fail("Curve geometry changed");
    auto verify_indices=[](const ProfileJson& a,const uint32_t* values,uint32_t count) {
        if (!a.is_array() || a.size()!=count) fail("Curve geometry changed");
        for (uint32_t i=0;i<count;++i) if (uint32(a[i])!=values[i]) fail("Curve geometry changed");
    };
    verify_indices(g["gpu_indices"],l.gpu_idx,l.gpu_count); verify_indices(g["other_indices"],l.other_idx,l.other_count);
    const auto& points=profile["points"];
    if (!points.is_array() || points.size()!=d.curve.size()) fail("Curve profile point count changed");
    std::set<uint32_t> seen; std::vector<std::pair<uint32_t,int32_t>> changes;
    for (const auto& item:points) {
        keys(item,{"idx","volt_mv","delta_khz"}); const auto idx=uint32(item["idx"]);
        const auto current=std::find_if(d.curve.begin(),d.curve.end(),[&](const auto& p){return p.idx==idx;});
        if (current==d.curve.end() || !seen.insert(idx).second || voltage(item["volt_mv"])!=current->volt_mv)
            fail("Curve point identity or voltage changed");
        const auto delta=integer(item["delta_khz"]);
        if (delta<INT32_MIN || delta>INT32_MAX || std::abs(delta)>d.curve_max_abs_delta_khz)
            fail("Curve profile delta outside current envelope");
        changes.emplace_back(idx,static_cast<int32_t>(delta));
    }
    // Preserve the existing undo/redo history; only mutate a copy after validation.
    const bool any_change=std::any_of(changes.begin(),changes.end(),[&](const auto& change) {
        const auto point=std::find_if(d.curve.begin(),d.curve.end(),[&](const auto& p){ return p.idx==change.first; });
        return point->delta_khz!=change.second;
    });
    if (!any_change) return; // Preserve existing undo/redo history on a no-op.
    staged=draft; staged.begin_gesture();
    for (const auto& change:changes) if (!staged.stage_khz(change.first,change.second)) fail("Curve profile could not be staged");
    staged.end_gesture(); draft=std::move(staged);
}
}
