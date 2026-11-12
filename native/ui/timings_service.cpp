/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "timings_service.h"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <regex>
#include <set>
#include <sstream>
namespace druta::ui {
namespace {
struct Guard { TimingsApi& a; dc_gpu* g; bool held; Guard(TimingsApi& api,dc_gpu* gpu):a(api),g(gpu),held(a.lock&&a.unlock&&g&&a.lock(g)==DC_OK){} ~Guard(){if(held)a.unlock(g);} };
struct Pin { TimingTransport& t; bool held=false; explicit Pin(TimingTransport& x):t(x){} ~Pin(){if(held)t.unpin();} };
bool same_id(const dc_gpu_identity& a,const dc_gpu_identity& b) {
    return a.slot[0] && b.slot[0] && (b.nvapi_ok||b.nvml_ok) && std::strcmp(a.slot,b.slot)==0 &&
      std::strcmp(a.uuid,b.uuid)==0 && a.nvapi_handle==b.nvapi_handle && a.nvapi_devid==b.nvapi_devid &&
      a.nvapi_subsys==b.nvapi_subsys && a.nvapi_selected==b.nvapi_selected && a.nvml_selected==b.nvml_selected &&
      a.nvml_has_devid==b.nvml_has_devid && a.nvml_devid==b.nvml_devid && a.nvml_subsys==b.nvml_subsys &&
      !a.pairing_error[0] && !b.pairing_error[0];
}
bool identity(TimingsApi& api,dc_gpu* gpu,const dc_gpu_identity& expected) {
    if (!api.identity) return false;
    for(int i=0;i<2;++i){dc_gpu_identity id{}; id.size=sizeof(id); if(api.identity(gpu,&id)==DC_OK && same_id(expected,id))return true;} return false;
}
bool same_slot(const std::string& a,const std::string& b) {
    static const std::regex re("^([0-9A-Fa-f]{4}|[0-9A-Fa-f]{8}):([0-9A-Fa-f]{2}):([0-9A-Fa-f]{2})\\.([0-7])$");
    std::smatch x,y; if(!std::regex_match(a,x,re)||!std::regex_match(b,y,re))return false;
    for(size_t i=1;i<5;++i)if(std::stoul(x[i],nullptr,16)!=std::stoul(y[i],nullptr,16))return false;
    return true;
}
bool run_ok(const TimingProcessResult& r) { return r.launched && !r.timed_out && !r.exit_code && r.error.empty(); }
bool fields_parse(const std::string& text,std::vector<TimingField>& out) {
    static const std::regex row("^([A-Za-z][A-Za-z0-9_]*)\\s+([A-Za-z][A-Za-z0-9_]*)\\s+\\[([0-9]+):([0-9]+)\\]\\s+([0-9]+)\\s+(.+?)\\s*$");
    static const std::regex note("^\\s+([A-Za-z][A-Za-z0-9_]*)\\s+INFERRED:.*$");
    std::istringstream input(text); std::string line; std::smatch m; std::set<std::string> names,inferred;
    try { while(std::getline(input,line)) { if(!line.empty()&&line.back()=='\r')line.pop_back();
        if(std::regex_match(line,m,note)){inferred.insert(m[1]);continue;}
        if(!std::regex_match(line,m,row) || m[1]=="FIELD")continue;
        const auto hi=std::stoul(m[3]),lo=std::stoul(m[4]); const auto max=std::stoull(m[5]);
        if(hi>=32||lo>hi||max!=((uint64_t{1}<<(hi-lo+1))-1)||!names.insert(m[1]).second)return false;
        TimingField f; f.name=m[1];f.reg=m[2];f.hi=static_cast<uint32_t>(hi);f.lo=static_cast<uint32_t>(lo);
        f.maximum=static_cast<uint32_t>(max);f.description=m[6];f.structural=f.description.find("[structural]")!=std::string::npos;out.push_back(f);
    } } catch(...) {return false;}
    for(auto& f:out)f.inferred=inferred.count(f.reg)!=0;
    for(size_t i=0;i<out.size();++i)for(size_t j=0;j<i;++j)
        if(out[i].reg==out[j].reg && out[i].lo<=out[j].hi && out[j].lo<=out[i].hi)
            out[i].overlapping=out[j].overlapping=true;
    return !out.empty();
}
bool dump_parse(const std::string& text,const std::string& slot,TimingSnapshot& out) {
    static const std::regex device("^(\\S+)\\s+(\\S+)\\s+\\([^\\n]*\\)\\s*$");
    static const std::regex scope("^\\s*\\[([^\\]]+)\\]\\s+base\\s+0x[0-9a-fA-F]+\\s*$");
    static const std::regex row("^\\s*([A-Za-z0-9_]+)\\s+@(0x[0-9a-fA-F]+)\\s*=\\s*(0x[0-9a-fA-F]+)(?:\\s+\\(([A-Za-z0-9_]+)\\))?\\s*$");
    std::istringstream input(text);std::string line;std::smatch m;bool found=false,broadcast=false;std::set<std::string> names;std::set<uint32_t> offsets;
    try {while(std::getline(input,line)){if(!line.empty()&&line.back()=='\r')line.pop_back();if(line.find_first_not_of(" \t")==std::string::npos)continue;
        if(std::regex_match(line,m,device)){if(found||!same_slot(m[1],slot))return false;found=true;out.slot=m[1];out.codename=m[2];continue;}
        if(std::regex_match(line,m,scope)){if(!found||broadcast||m[1]!="broadcast")return false;broadcast=true;continue;}
        if(!broadcast||!std::regex_match(line,m,row))return false;
        const auto off=std::stoull(m[2],nullptr,16),word=std::stoull(m[3],nullptr,16);
        if(off>UINT32_MAX||word>UINT32_MAX||off%4||!names.insert(m[1]).second||!offsets.insert(static_cast<uint32_t>(off)).second)return false;
        TimingWord w;w.name=m[1];w.offset=static_cast<uint32_t>(off);w.value=static_cast<uint32_t>(word);w.confidence=m[4];out.broadcast.push_back(w);
    }} catch(...){return false;}
    return found && broadcast && !out.broadcast.empty();
}
bool known_layout(std::string name) {
    std::transform(name.begin(),name.end(),name.begin(),[](unsigned char c){return static_cast<char>(std::toupper(c));});
    return !name.empty() && name.rfind("UNKNOWN",0)!=0 && name.rfind("UNSUPPORTED",0)!=0 && name.rfind("GENERIC",0)!=0;
}
void state(TimingsApi& api,dc_gpu* gpu,std::optional<uint32_t>& memory,std::optional<uint32_t>& pstate,std::optional<int32_t>& offset) {
    if(!api.telemetry)return; dc_telemetry t{};t.size=sizeof(t);
    if(api.telemetry(gpu,&t)!=DC_OK)return;
    if(t.present&DC_T_MEM)memory=t.mem;if(t.present&DC_T_PSTATE)pstate=t.pstate;if(t.present&DC_T_MEM_OFF)offset=t.mem_off;
}
TimingSnapshot capture_locked(TimingsApi& api,dc_gpu* gpu,const dc_gpu_identity& id,TimingTransport& t) {
    TimingSnapshot s; if(!identity(api,gpu,id)){s.message="GPU identity unavailable";return s;}
    std::optional<int32_t> before_offset,after_offset;
    state(api,gpu,s.memory_before_mhz,s.pstate_before,before_offset);
    const auto f=t.run(TimingCommand::fields,"","",0,false,false);
    if(!run_ok(f)||!fields_parse(f.output,s.fields)){s.message="Helper fields are unavailable or malformed";return s;}
    const auto d=t.run(TimingCommand::dump,id.slot,"",0,false,false);
    state(api,gpu,s.memory_after_mhz,s.pstate_after,after_offset);
    if(!run_ok(d)||!dump_parse(d.output,id.slot,s)){s.message="Selected-card raw dump is unavailable or malformed";return s;}
    if(!identity(api,gpu,id)){s.message="GPU identity changed during capture";return s;}
    s.offset_before=before_offset; s.offset_after=after_offset;
    s.memory_clock_stable=s.memory_before_mhz && s.memory_after_mhz && *s.memory_before_mhz==*s.memory_after_mhz;
    dc_capabilities caps{};caps.size=sizeof(caps);
    if(s.memory_clock_stable && s.pstate_before && s.pstate_after && (*s.pstate_before==0||*s.pstate_before==2) &&
       (*s.pstate_after==0||*s.pstate_after==2) && before_offset && after_offset && *before_offset==*after_offset &&
       api.capabilities && api.capabilities(gpu,&caps)==DC_OK && caps.mem_clock_count && caps.mem_clock_count<=512) {
        const auto top=*std::max_element(caps.mem_clocks,caps.mem_clocks+caps.mem_clock_count);
        s.top_band=top>0 && static_cast<double>(*s.memory_before_mhz)-*before_offset/2.0>=static_cast<double>(top)-5.0;
    }
    s.ok=true;s.message=known_layout(s.codename)?"Selected-card raw timing registers captured":"Unknown layout; raw registers only";return s;
}
struct Contract {bool valid=false,preview=false,commit=false;};
Contract contract(const std::string& h) {
    const bool legacy=h.find("Everything defaults to a dry run")!=std::string::npos && h.find("--commit is required to touch hardware")!=std::string::npos;
    const bool direct=h.find("nvtune - NVIDIA FBPA memory timing tool")!=std::string::npos &&
      std::regex_search(h,std::regex("(^|\\n)\\s*set FIELD=VALUE\\.\\.\\.\\s+write fields\\s*(\\r?\\n|$)")) &&
      h.find("A first write snapshots stock values")!=std::string::npos && h.find("--commit")==std::string::npos;
    const bool dry=std::regex_search(h,std::regex("(^|\\n)\\s*(?:-\\w,\\s*)?--dry-run\\b"));
    const bool commit=std::regex_search(h,std::regex("(^|\\n)\\s*(?:-\\w,\\s*)?--commit\\b"));
    return {legacy||direct||(dry&&commit),dry,legacy||commit};
}
bool preview_matches(const std::string& text,const TimingPlan& plan) {
    static const std::regex operation("^\\s*([A-Za-z0-9_]+)\\s+@(0x[0-9a-fA-F]+)\\s+(0x[0-9a-fA-F]+)\\s*->\\s*(0x[0-9a-fA-F]+)\\s*\\[would write\\]\\s*$");
    static const std::regex change("^\\s+([A-Za-z0-9_]+)\\s+([0-9]+)\\s*->\\s*([0-9]+)\\s*$");
    static const std::regex device("^(\\S+)\\s+(\\S+)\\s+\\([^\\n]*\\)\\s*$");
    std::istringstream lines(text); std::string line; std::smatch m; unsigned ops=0,changes=0,devices=0,scopes=0;
    try { while(std::getline(lines,line)) {
        if(!line.empty()&&line.back()=='\r')line.pop_back();
        if(line.find_first_not_of(" \t")==std::string::npos)continue;
        if(std::regex_match(line,m,operation)) {
            if(++ops!=1||m[1]!=plan.reg||std::stoull(m[2],nullptr,16)!=plan.offset||
               std::stoull(m[3],nullptr,16)!=plan.prior_word||std::stoull(m[4],nullptr,16)!=plan.target_word)return false;
            continue;
        }
        if(std::regex_match(line,m,change)) {
            if(!ops||++changes!=1||m[1]!=plan.field||std::stoull(m[2])!=plan.prior||std::stoull(m[3])!=plan.value)return false;
            continue;
        }
        const auto first=line.find_first_not_of(" \t");const auto trimmed=line.substr(first);
        if(std::regex_match(trimmed,m,device)){if(++devices!=1 || !same_slot(m[1],plan.slot))return false;continue;}
        if(trimmed=="[broadcast]"){if(++scopes!=1)return false;continue;}
        if(trimmed=="dry run complete: no registers written"||trimmed.rfind("reminder:",0)==0)continue;
        return false; // warnings or unknown output cannot authorize a write
    }} catch(...) {return false;}
    return ops==1 && changes==1;
}TimingPlan make_plan(const TimingSnapshot& s,const std::string& field,uint32_t value,const std::string& token) {
    TimingPlan p;p.slot=s.slot;p.field=field;p.value=value;p.helper_token=token;
    if(!s.ok||!s.top_band||!known_layout(s.codename)){p.message="Fresh top-band capture and known layout required";return p;}
    auto f=std::find_if(s.fields.begin(),s.fields.end(),[&](const auto& x){return x.name==field;});
    if(f==s.fields.end()||f->structural||f->inferred||f->overlapping||value>f->maximum){p.message="Field is unavailable, structural, inferred, overlapping, or outside its range";return p;}
    auto w=std::find_if(s.broadcast.begin(),s.broadcast.end(),[&](const auto& x){return x.name==f->reg;});
    if(w==s.broadcast.end()||(!w->confidence.empty() && w->confidence!="DOCUMENTED")){p.message="Register geometry is unavailable or unconfirmed";return p;}
    p.reg=f->reg;p.offset=w->offset;p.mask=f->maximum<<f->lo;p.prior_word=w->value;p.prior=(w->value&p.mask)>>f->lo;
    p.target_word=(w->value&~p.mask)|((value<<f->lo)&p.mask);p.no_change=p.target_word==p.prior_word;p.ok=true;p.message=p.no_change?"Field already matches":"Selected-field preview ready";return p;
}
}
TimingsService::TimingsService(TimingsApi a,dc_gpu* gpu,const dc_gpu_identity& id,TimingTransport& t):api_(a),gpu_(gpu),target_(id),transport_(t){}
TimingSnapshot TimingsService::capture(){Guard g(api_,gpu_);TimingSnapshot s;if(!g.held){s.message="GPU lock unavailable";return s;} Pin p(transport_);std::string error;if(!(p.held=transport_.pin(error))){s.message=error;return s;}return capture_locked(api_,gpu_,target_,transport_);}
TimingPlan TimingsService::preview(const std::string& field,uint32_t value) {
    Guard g(api_,gpu_);TimingPlan p;if(!g.held){p.message="GPU lock unavailable";return p;}Pin pin(transport_);std::string error;
    if(!(pin.held=transport_.pin(error))){p.message=error;return p;}
    auto h=transport_.run(TimingCommand::help,"","",0,false,false);if(!run_ok(h)||!contract(h.output).valid){p.message="Unrecognized helper command contract";return p;}
    auto snap=capture_locked(api_,gpu_,target_,transport_);return make_plan(snap,field,value,transport_.token());
}
TimingResult TimingsService::apply(const TimingPlan& plan,const std::function<void(const TimingPlan&)>& before_write) {
    TimingResult result;if(!plan.ok){result.message="Preview is invalid";return result;}
    Guard g(api_,gpu_);if(!g.held){result.message="GPU lock unavailable";return result;}
    Pin pin(transport_);std::string error;if(!(pin.held=transport_.pin(error)) || transport_.token()!=plan.helper_token){result.message="Helper changed since preview";return result;}
    auto h=transport_.run(TimingCommand::help,"","",0,false,false);const auto c=contract(h.output);
    if(!run_ok(h)||!c.valid){result.message="Unrecognized helper command contract";return result;}
    auto snap=capture_locked(api_,gpu_,target_,transport_);auto fresh=make_plan(snap,plan.field,plan.value,transport_.token());
    if(!fresh.ok||!same_slot(plan.slot,fresh.slot)||fresh.reg!=plan.reg||fresh.offset!=plan.offset||fresh.mask!=plan.mask||fresh.prior_word!=plan.prior_word||fresh.target_word!=plan.target_word){result.message="Timing baseline, target or operating band changed; no write issued";return result;}
    if(fresh.no_change){result.ok=true;result.message="Timing already matches";return result;}
    // Where supported, run the helper's own preview as well as our selected-field plan.
    if(c.preview||c.commit){auto pre=transport_.run(TimingCommand::preview,target_.slot,plan.field,plan.value,false,c.preview);
        if(!run_ok(pre)||!preview_matches(pre.output,plan)){result.message="Helper preview failed or has warnings; no write issued";return result;}}
    auto guarded=capture_locked(api_,gpu_,target_,transport_);auto again=make_plan(guarded,plan.field,plan.value,transport_.token());
    if(!again.ok||again.reg!=fresh.reg||again.offset!=fresh.offset||again.prior_word!=fresh.prior_word||
       again.mask!=fresh.mask||again.target_word!=fresh.target_word||!identity(api_,gpu_,target_)||!transport_.unchanged(error)){result.message="Pre-commit read or helper identity changed; no write issued";return result;}
    TimingPlan undo=fresh;undo.value=fresh.prior;undo.prior=fresh.value;undo.prior_word=fresh.target_word;undo.target_word=fresh.prior_word;undo.no_change=false;
    if(before_write)before_write(undo);result.undo=undo;
    const auto write=transport_.run(TimingCommand::commit,target_.slot,plan.field,plan.value,c.commit,false);
    result.may_have_changed=write.launched; if(!write.launched)result.undo.reset();
    auto after=capture_locked(api_,gpu_,target_,transport_);
    auto row=std::find_if(after.broadcast.begin(),after.broadcast.end(),[&](const auto& w){return w.name==plan.reg && w.offset==plan.offset;});
    result.ok=run_ok(write)&&after.ok&&after.top_band&&after.memory_before_mhz==guarded.memory_before_mhz&&
        after.memory_after_mhz==guarded.memory_after_mhz&&after.pstate_before==guarded.pstate_before&&
        after.pstate_after==guarded.pstate_after&&after.offset_before==guarded.offset_before&&
        after.offset_after==guarded.offset_after&&row!=after.broadcast.end()&&row->value==plan.target_word;
    result.message=result.ok?"Selected timing request and preserved register bits read back exactly":"Timing write failed or readback is uncertain; retain Undo";
    return result;
}
TimingResult TimingsService::restore(const TimingPlan& undo,const std::function<void(const TimingPlan&)>& before_write) {
    const auto current=preview(undo.field,undo.value);
    if(current.ok && current.no_change && current.helper_token==undo.helper_token && same_slot(current.slot,undo.slot) &&
       current.reg==undo.reg && current.offset==undo.offset && current.mask==undo.mask && current.target_word==undo.target_word) {
        TimingResult result; result.ok=true; result.message="Captured timing already reads back exactly"; return result;
    }
    return apply(undo,before_write);
}
}
