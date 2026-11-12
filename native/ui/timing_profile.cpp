/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "timing_profile.h"
#include <algorithm>
#include <cctype>
#include <cstring>
#include <regex>
#include <set>
#include <stdexcept>

namespace druta::ui {
namespace {
[[noreturn]] void fail(const char* reason) { throw std::runtime_error(reason); }
void keys(const ProfileJson& obj,std::initializer_list<const char*> fields) {
    if (!obj.is_object() || obj.size()!=fields.size()) fail("Invalid timing profile fields");
    for (const char* field:fields) if (!obj.contains(field)) fail("Missing timing profile field");
}
int64_t integer(const ProfileJson& j) {
    if (j.is_number_unsigned()) { const auto x=j.get<uint64_t>(); if(x>INT64_MAX) fail("Timing integer out of range"); return static_cast<int64_t>(x); }
    if (!j.is_number_integer()) fail("Timing value must be an integer"); return j.get<int64_t>();
}
uint32_t unsigned_value(const ProfileJson& j) {
    const auto x=integer(j); if(x<0 || x>UINT32_MAX)fail("Timing integer out of range"); return static_cast<uint32_t>(x);
}
std::string string_value(const ProfileJson& j) {
    if(!j.is_string())fail("Timing profile string required"); auto s=j.get<std::string>(); if(s.empty() || s.find('\0')!=std::string::npos)fail("Empty or invalid timing profile string"); return s;
}
template<size_t N> std::string id_string(const char (&value)[N]) {
    const auto end=std::find(value,value+N,'\0'); if(end==value || end==value+N)fail("GPU identity unavailable"); return std::string(value,end);
}
bool same_slot(const std::string& a,const std::string& b) {
    static const std::regex re("^([0-9A-Fa-f]{4}|[0-9A-Fa-f]{8}):([0-9A-Fa-f]{2}):([0-9A-Fa-f]{2})\\.([0-7])$");
    std::smatch x,y; if(!std::regex_match(a,x,re)||!std::regex_match(b,y,re))return false;
    for(size_t i=1;i<5;++i) if(std::stoul(x[i],nullptr,16)!=std::stoul(y[i],nullptr,16))return false; return true;
}
void validate_snapshot(const dc_gpu_identity& id,const TimingSnapshot& s) {
    const auto slot=id_string(id.slot); (void)id_string(id.uuid);
    if ((!id.nvapi_ok && !id.nvml_ok) || id.pairing_error[0] || !same_slot(slot,s.slot)) fail("Timing target identity is unavailable or mismatched");
    std::string code=s.codename; std::transform(code.begin(),code.end(),code.begin(),[](unsigned char c){return static_cast<char>(std::toupper(c));});
    if(!s.ok || !s.top_band || !s.memory_clock_stable || code.empty() || code.rfind("UNKNOWN",0)==0 ||
       code.rfind("UNSUPPORTED",0)==0 || code.rfind("GENERIC",0)==0 ||
       !s.memory_before_mhz || !s.memory_after_mhz || !*s.memory_before_mhz || s.memory_before_mhz!=s.memory_after_mhz ||
       !s.pstate_before || !s.pstate_after || (*s.pstate_before!=0 && *s.pstate_before!=2) ||
       (*s.pstate_after!=0 && *s.pstate_after!=2) || !s.offset_before || !s.offset_after || s.offset_before!=s.offset_after)
        fail("Fresh qualified top-band timing capture required");
    std::set<std::string> names, regs; std::set<uint32_t> offsets;
    for(const auto& w:s.broadcast) if(w.name.empty() || w.offset%4 || !regs.insert(w.name).second || !offsets.insert(w.offset).second)
        fail("Ambiguous timing register geometry");
    for(const auto& f:s.fields) {
        if(f.name.empty() || f.reg.empty() || !names.insert(f.name).second || f.hi>=32 || f.lo>f.hi) fail("Ambiguous timing field geometry");
        const uint64_t maximum=(uint64_t{1} << (f.hi-f.lo+1))-1;
        if(f.maximum!=maximum)fail("Invalid timing field width");
    }
}
struct Binding { const TimingField* field; const TimingWord* word; };
Binding bind(const TimingSnapshot& s,const TimingProfileRequest& r) {
    const auto f=std::find_if(s.fields.begin(),s.fields.end(),[&](const auto& x){return x.name==r.field;});
    if(f==s.fields.end() || f->structural || f->inferred || f->overlapping || r.value>f->maximum)
        fail("Timing field unavailable, unsafe or out of range");
    for(const auto& other:s.fields) if(&other!=&*f && other.reg==f->reg && other.lo<=f->hi && f->lo<=other.hi)
        fail("Timing field overlaps another field");
    const auto w=std::find_if(s.broadcast.begin(),s.broadcast.end(),[&](const auto& x){return x.name==f->reg;});
    if(w==s.broadcast.end() || (!w->confidence.empty() && w->confidence!="DOCUMENTED"))fail("Timing register unavailable or unconfirmed");
    return {&*f,&*w};
}
ProfileJson band(const TimingSnapshot& s) {
    return {{"memory_mhz",*s.memory_before_mhz},{"pstate_before",*s.pstate_before},
        {"pstate_after",*s.pstate_after},{"memory_offset",*s.offset_before}};
}
}
ProfileJson capture_timing_profile(const dc_gpu_identity& id,const TimingSnapshot& snapshot,const std::vector<TimingProfileRequest>& requests) {
    validate_snapshot(id,snapshot); if(requests.empty())fail("Timing profile requires at least one field");
    std::set<std::string> seen; ProfileJson fields=ProfileJson::array();
    for(const auto& r:requests) {
        if(!seen.insert(r.field).second)fail("Duplicate timing field"); const auto b=bind(snapshot,r);
        fields.push_back({{"field",r.field},{"value",r.value},{"register",b.field->reg},{"offset",b.word->offset},
            {"lo",b.field->lo},{"hi",b.field->hi},{"maximum",b.field->maximum}});
    }
    return {{"format","druta-native-timing"},{"version",1},{"target",{{"uuid",id_string(id.uuid)},{"slot",id_string(id.slot)}}},
        {"codename",snapshot.codename},{"band",band(snapshot)},{"fields",std::move(fields)}};
}
std::vector<TimingProfileRequest> validate_timing_profile(const ProfileJson& p,const dc_gpu_identity& id,const TimingSnapshot& s) {
    validate_snapshot(id,s); keys(p,{"format","version","target","codename","band","fields"});
    if(string_value(p["format"])!="druta-native-timing" || integer(p["version"])!=1)fail("Unsupported timing profile format");
    const auto& target=p["target"]; keys(target,{"uuid","slot"});
    if(string_value(target["uuid"])!=id_string(id.uuid) || !same_slot(string_value(target["slot"]),id_string(id.slot)) ||
       string_value(p["codename"])!=s.codename) fail("Timing profile target or layout changed");
    const auto& b=p["band"]; keys(b,{"memory_mhz","pstate_before","pstate_after","memory_offset"});
    if(unsigned_value(b["memory_mhz"])!=*s.memory_before_mhz || unsigned_value(b["pstate_before"])!=*s.pstate_before ||
       unsigned_value(b["pstate_after"])!=*s.pstate_after || integer(b["memory_offset"])!=*s.offset_before)
        fail("Timing profile operating band changed");
    const auto& fields=p["fields"]; if(!fields.is_array() || fields.empty() || fields.size()>s.fields.size())fail("Invalid timing profile field list");
    std::set<std::string> seen; std::vector<TimingProfileRequest> result; result.reserve(fields.size());
    for(const auto& item:fields) {
        keys(item,{"field","value","register","offset","lo","hi","maximum"});
        TimingProfileRequest request{string_value(item["field"]),unsigned_value(item["value"])};
        if(!seen.insert(request.field).second)fail("Duplicate timing field"); const auto binding=bind(s,request);
        if(string_value(item["register"])!=binding.field->reg || unsigned_value(item["offset"])!=binding.word->offset ||
           unsigned_value(item["lo"])!=binding.field->lo || unsigned_value(item["hi"])!=binding.field->hi ||
           unsigned_value(item["maximum"])!=binding.field->maximum) fail("Timing field geometry changed");
        result.push_back(std::move(request));
    }
    return result;
}
ProfileJson capture_timing_editor_profile(const dc_gpu_identity& id,const TimingSnapshot& s,const std::vector<TimingRequest>& staged){
    validate_snapshot(id,s);std::vector<TimingRequest> values;
    for(const auto& f:s.fields){
        if(f.structural||f.inferred||f.overlapping)continue;
        const auto w=std::find_if(s.broadcast.begin(),s.broadcast.end(),[&](const auto& word){return word.name==f.reg;});
        if(w==s.broadcast.end()||(!w->confidence.empty()&&w->confidence!="DOCUMENTED"))continue;
        bool overlap=false;for(const auto& other:s.fields)if(&other!=&f&&other.reg==f.reg&&other.lo<=f.hi&&f.lo<=other.hi)overlap=true;
        if(!overlap)values.push_back({f.name,(w->value>>f.lo)&f.maximum});
    }
    std::set<std::string> seen;for(const auto& request:staged){
        if(!seen.insert(request.field).second)fail("Duplicate staged timing field");
        (void)bind(s,request);const auto found=std::find_if(values.begin(),values.end(),[&](const auto& r){return r.field==request.field;});
        if(found==values.end())fail("Staged field has no qualified captured value");found->value=request.value;
    }
    std::sort(values.begin(),values.end(),[](const auto& a,const auto& b){return a.field<b.field;});
    return capture_timing_profile(id,s,values);
}
}
