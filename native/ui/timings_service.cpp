/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "timings_service.h"
#include "vendor/json/json.hpp"
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
std::string process_error(const char* phase,const TimingProcessResult& r){
    std::string reason=!r.launched?"helper could not start":r.timed_out?"helper timed out":r.exit_code?"helper ran but the operation failed":"helper output unavailable";
    const auto detail=!r.error.empty()?r.error:r.output;
    return std::string(phase)+": "+reason+(detail.empty()?"":"; "+detail.substr(0,700));
}
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
bool diagnostic_capture(TimingsApi& api,dc_gpu* gpu,const dc_gpu_identity& id,TimingTransport& t,TimingSnapshot& s) {
    dc_capabilities caps{};caps.size=sizeof(caps);
    if(api.capabilities && api.capabilities(gpu,&caps)==DC_OK && (caps.present&DC_C_MEM_DIV) && caps.mem_div) {
        s.diagnostic_evidence.memory_clock_divisor=caps.mem_div;
        s.diagnostic_evidence.divisor_provenance="Current adapter memory type capability";
    }
    // Released nvtune decoder treats parsed fields on a known layout as cycle
    // counts. Split fragments and RFC/WL must not become time values.
    static const std::vector<std::string> suffixes={"_LO","_HI","_MSB","_LSB","_MID"};
    if(known_layout(s.codename))for(const auto& f:s.fields) {
        if(f.structural || f.inferred || f.overlapping)continue;
        bool split=false;
        for(const auto& suffix:suffixes) {
            for(const auto& other:s.fields)if(other.name==f.name+suffix)split=true;
            if(f.name.size()>suffix.size() && f.name.compare(f.name.size()-suffix.size(),suffix.size(),suffix)==0) {
                const auto head=f.name.substr(0,f.name.size()-suffix.size());
                for(const auto& other:s.fields)if(other.name!=f.name && (other.name==head || other.name.rfind(head+"_",0)==0))split=true;
            }
        }
        s.diagnostic_evidence.units.push_back({f.name,f.reg,s.codename,f.lo,f.hi,true,!split && f.name!="RFC" && f.name!="WL","nvtune field metadata; released timing decoder"});
    }
    std::optional<uint32_t> before,after,pb,pa;std::optional<int32_t> ob,oa;
    state(api,gpu,before,pb,ob);const auto saved=t.run(TimingCommand::save,id.slot,"",0,false,false);state(api,gpu,after,pa,oa);
    if(!identity(api,gpu,id))return false;
    if(!run_ok(saved)||!before||!after||*before!=*after||!s.memory_clock_stable || !s.memory_before_mhz || *before!=*s.memory_before_mhz ||
       !pb || !pa || !s.pstate_before || !s.pstate_after || *pb!=*pa || *pb!=*s.pstate_before || *pb!=*s.pstate_after ||
       !ob || !oa || !s.offset_before || !s.offset_after || *ob!=*oa || *ob!=*s.offset_before || *ob!=*s.offset_after)return true;
    try {
        const auto doc=nlohmann::json::parse(saved.output);if(!doc.is_object() || !doc.contains("slot") || !doc["slot"].is_string() || !same_slot(doc["slot"].get<std::string>(),s.slot) ||
            !doc.contains("codename") || !doc["codename"].is_string() || doc["codename"].get<std::string>()!=s.codename || !doc.contains("registers") || !doc["registers"].is_object())return true;
        const auto& registers=doc["registers"];if(!registers.contains("broadcast") || !registers["broadcast"].is_object())return true;
        auto parse_word=[](const nlohmann::json& j,uint32_t& out){if(!j.is_string())return false;const auto text=j.get<std::string>();
            if(text.size()<3 || text.size()>10 || text[0]!='0' || (text[1]!='x'&&text[1]!='X') || text.find_first_not_of("0123456789abcdefABCDEF",2)!=std::string::npos)return false;
            try{out=static_cast<uint32_t>(std::stoul(text,nullptr,16));return true;}catch(...){return false;}};
        const auto& broadcast=registers["broadcast"];
        for(const auto& w:s.broadcast){uint32_t value=0;if(!broadcast.contains(w.name)||!parse_word(broadcast[w.name],value)||value!=w.value)return true;}
        std::vector<TimingPartition> partitions;
        for(auto it=registers.begin();it!=registers.end();++it){if(it.key()=="broadcast")continue;
            if(!std::regex_match(it.key(),std::regex("fbpa[0-9]{1,3}")) || !it.value().is_object() || partitions.size()>=256)return true;
            TimingPartition part;part.scope=it.key();
            for(auto word=it.value().begin();word!=it.value().end();++word){if(part.words.size()>=256 || !std::regex_match(word.key(),std::regex("[A-Za-z][A-Za-z0-9_]{0,63}")))return true;
                uint32_t value=0;if(!parse_word(word.value(),value))return true;TimingWord entry;entry.name=word.key();entry.value=value;part.words.push_back(entry);}
            partitions.push_back(std::move(part));
        }
        s.diagnostic_evidence.partitions=std::move(partitions);
    }catch(...){return true;}
    return true;
}
TimingSnapshot capture_locked(TimingsApi& api,dc_gpu* gpu,const dc_gpu_identity& id,TimingTransport& t) {
    TimingSnapshot s; if(!identity(api,gpu,id)){s.message="GPU identity unavailable";return s;}
    std::optional<int32_t> before_offset,after_offset;
    state(api,gpu,s.memory_before_mhz,s.pstate_before,before_offset);
    const auto f=t.run(TimingCommand::fields,"","",0,false,false);
    if(!run_ok(f)){s.message=process_error("Timing metadata",f);return s;}
    if(!fields_parse(f.output,s.fields)){s.message="Helper field metadata is malformed";return s;}
    const auto d=t.run(TimingCommand::dump,id.slot,"",0,false,false);
    state(api,gpu,s.memory_after_mhz,s.pstate_after,after_offset);
    if(!run_ok(d)){s.message=process_error("Selected-card timing read",d);return s;}
    if(!dump_parse(d.output,id.slot,s)){s.message="Selected-card raw dump is malformed or targets another adapter";return s;}
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
TimingSnapshot TimingsService::capture(){Guard g(api_,gpu_);TimingSnapshot s;if(!g.held){s.message="GPU lock unavailable";return s;} Pin p(transport_);std::string error;if(!(p.held=transport_.pin(error))){s.message="Helper unavailable: "+error;return s;}s=capture_locked(api_,gpu_,target_,transport_);if(s.ok) {
    if(!diagnostic_capture(api_,gpu_,target_,transport_,s)){s.ok=false;s.message="GPU identity unavailable after timing diagnostics";s.diagnostic_evidence={};return s;}
    if(!s.diagnostic_evidence.partitions.empty()){last_diagnostic_=s;last_diagnostic_token_=transport_.token();}
    else if(last_diagnostic_ && last_diagnostic_token_==transport_.token() && same_slot(last_diagnostic_->slot,s.slot) && last_diagnostic_->codename==s.codename && last_diagnostic_->broadcast.size()==s.broadcast.size()) {
        bool same=true;for(size_t i=0;i<s.broadcast.size();++i)if(s.broadcast[i].name!=last_diagnostic_->broadcast[i].name || s.broadcast[i].offset!=last_diagnostic_->broadcast[i].offset || s.broadcast[i].value!=last_diagnostic_->broadcast[i].value)same=false;
        if(same){s.diagnostic_evidence.partitions=last_diagnostic_->diagnostic_evidence.partitions;s.diagnostic_evidence.partitions_stale=true;}
    }
}remember_initial(s,transport_.token());return s;}
TimingPlan TimingsService::preview(const std::string& field,uint32_t value) {
    Guard g(api_,gpu_);TimingPlan p;if(!g.held){p.message="GPU lock unavailable";return p;}Pin pin(transport_);std::string error;
    if(!(pin.held=transport_.pin(error))){p.message=error;return p;}
    auto h=transport_.run(TimingCommand::help,"","",0,false,false);if(!run_ok(h)||!contract(h.output).valid){p.message="Unrecognized helper command contract";return p;}
    auto snap=capture_locked(api_,gpu_,target_,transport_);remember_initial(snap,transport_.token());return make_plan(snap,field,value,transport_.token());
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
namespace {
bool same_band(const TimingSnapshot& a,const TimingSnapshot& b){
    return a.ok&&b.ok&&a.top_band&&b.top_band&&a.codename==b.codename&&same_slot(a.slot,b.slot)&&
        a.memory_before_mhz==b.memory_before_mhz&&a.memory_after_mhz==b.memory_after_mhz&&
        a.pstate_before==b.pstate_before&&a.pstate_after==b.pstate_after&&a.offset_before==b.offset_before&&a.offset_after==b.offset_after;
}
bool same_field_plan(const TimingPlan& a,const TimingPlan& b,bool words=true){
    return a.ok&&b.ok&&a.field==b.field&&a.reg==b.reg&&a.offset==b.offset&&a.mask==b.mask&&
        (!words||(a.prior_word==b.prior_word&&a.target_word==b.target_word));
}
TimingBatchPlan make_batch(const TimingSnapshot& source,std::vector<TimingRequest> requests,const std::string& token){
    TimingBatchPlan batch;batch.slot=source.slot;batch.codename=source.codename;batch.helper_token=token;batch.baseline=source;
    if(requests.empty()||requests.size()>source.fields.size()){batch.message="Select at least one known timing field";return batch;}
    std::set<std::string> seen;auto working=source;batch.no_change=true;
    std::sort(requests.begin(),requests.end(),[](const auto& a,const auto& b){return a.field<b.field;});
    for(const auto& request:requests){
        if(!seen.insert(request.field).second){batch.message="Duplicate staged timing field";return batch;}
        auto plan=make_plan(working,request.field,request.value,token);
        if(!plan.ok){batch.message=request.field+": "+plan.message;return batch;}
        const auto selected=std::find_if(source.fields.begin(),source.fields.end(),[&](const auto& f){return f.name==request.field;});
        if(selected==source.fields.end()||selected->hi>=32||selected->lo>selected->hi||
            selected->maximum!=((uint64_t{1}<<(selected->hi-selected->lo+1))-1)){
            batch.message="Invalid selected field geometry";return batch;}
        for(const auto& other:source.fields)if(&other!=&*selected&&other.reg==selected->reg&&other.lo<=selected->hi&&selected->lo<=other.hi){batch.message="Selected timing field overlaps another field";return batch;}
        batch.no_change=batch.no_change&&plan.no_change;
        for(auto& word:working.broadcast)if(word.name==plan.reg)word.value=plan.target_word;
        batch.fields.push_back(std::move(plan));
    }
    batch.ok=true;batch.message=batch.no_change?"All selected fields already match":"Whole selected-field timing preview ready";return batch;
}
std::vector<TimingRequest> requests(const TimingBatchPlan& plan){
    std::vector<TimingRequest> result;for(const auto& f:plan.fields)result.push_back({f.field,f.value});return result;
}
bool same_batch(const TimingBatchPlan& a,const TimingBatchPlan& b){
    if(!a.ok||!b.ok||a.helper_token!=b.helper_token||!same_band(a.baseline,b.baseline)||a.fields.size()!=b.fields.size())return false;
    for(size_t i=0;i<a.fields.size();++i)if(!same_field_plan(a.fields[i],b.fields[i]))return false;return true;
}
bool preserved(const TimingSnapshot& before,const TimingSnapshot& after,const TimingPlan& step){
    if(!same_band(before,after)||before.broadcast.size()!=after.broadcast.size())return false;
    for(const auto& old:before.broadcast){const auto now=std::find_if(after.broadcast.begin(),after.broadcast.end(),[&](const auto& w){return w.name==old.name&&w.offset==old.offset;});
        if(now==after.broadcast.end()||now->value!=(old.name==step.reg?step.target_word:old.value))return false;}
    return true;
}
}
void TimingsService::remember_initial(const TimingSnapshot& s,const std::string& token){
    if(initial_&&token!=initial_token_){initial_.reset();initial_token_.clear();}
    if(!initial_&&s.ok&&s.top_band&&known_layout(s.codename)){initial_=s;initial_token_=token;}
}
TimingBatchPlan TimingsService::preview(const std::vector<TimingRequest>& selected){
    TimingBatchPlan p;if(transport_.pending()){p.message="Timing helper exit is unconfirmed";return p;}Guard guard(api_,gpu_);if(!guard.held){p.message="GPU lock unavailable";return p;}
    Pin pin(transport_);std::string error;if(!(pin.held=transport_.pin(error))){p.message="Helper unavailable: "+error;return p;}
    const auto help=transport_.run(TimingCommand::help,"","",0,false,false);
    if(!run_ok(help)||!contract(help.output).valid){p.message="Unrecognized helper command contract";return p;}
    const auto s=capture_locked(api_,gpu_,target_,transport_);remember_initial(s,transport_.token());
    p=make_batch(s,selected,transport_.token());if(!s.ok)p.message=s.message;return p;
}
TimingBatchResult TimingsService::apply_batch(const TimingBatchPlan& plan,const std::function<void(const TimingBatchPlan&)>& before_write){
    TimingBatchResult result;if(!plan.ok){result.message="Whole timing preview is invalid";return result;}
    const auto stopped=[&](const std::string& why){return why+(result.may_have_changed?"; issued selected restore retained":"; no writes issued");};
    try {
    if(transport_.pending()){result.message="Timing helper exit is unconfirmed";return result;}
    Guard guard(api_,gpu_);if(!guard.held){result.message="GPU lock unavailable";return result;}
    Pin pin(transport_);std::string error;if(!(pin.held=transport_.pin(error))||transport_.token()!=plan.helper_token){result.message="Helper changed since preview";return result;}
    const auto help=transport_.run(TimingCommand::help,"","",0,false,false);const auto c=contract(help.output);
    if(!run_ok(help)||!c.valid){result.message="Unrecognized helper command contract";return result;}
    auto current=capture_locked(api_,gpu_,target_,transport_);const auto fresh=make_batch(current,requests(plan),transport_.token());
    if(!same_batch(plan,fresh)){result.message="Timing baseline, geometry or operating band changed; no writes issued";return result;}
    // Check every helper preview before the first write. Same-register fields
    // are previewed independently against the current physical word here.
    if(c.preview||c.commit)for(const auto& field:fresh.fields){
        const auto single=make_plan(current,field.field,field.value,transport_.token());if(single.no_change)continue;
        const auto dry=transport_.run(TimingCommand::preview,target_.slot,single.field,single.value,false,c.preview);
        if(!run_ok(dry)||!preview_matches(dry.output,single)){result.message="A selected helper preview failed; no writes issued";return result;}
    }
    const auto before_dry=current;current=capture_locked(api_,gpu_,target_,transport_);
    if(!preserved(before_dry,current,TimingPlan{})){result.message="Register state changed during whole-profile dry run; no writes issued";return result;}
    if(!same_batch(fresh,make_batch(current,requests(plan),transport_.token()))){result.message="Selected timing baseline changed during preflight; no writes issued";return result;}
    TimingBatchPlan undo=fresh;undo.no_change=false;undo.fields.clear();
    for(const auto& expected:fresh.fields){
        const auto step=make_plan(current,expected.field,expected.value,transport_.token());
        if(!same_field_plan(expected,step)||!transport_.unchanged(error)||!identity(api_,gpu_,target_)){
            result.message=stopped("Timing request/geometry changed during transaction");return result;}
        if(step.no_change)continue;
        if(c.preview||c.commit){const auto dry=transport_.run(TimingCommand::preview,target_.slot,step.field,step.value,false,c.preview);
            if(!run_ok(dry)||!preview_matches(dry.output,step)){result.message=stopped("Helper preview failed during transaction");return result;}}
        const auto guarded=capture_locked(api_,gpu_,target_,transport_);const auto again=make_plan(guarded,step.field,step.value,transport_.token());
        if(!same_field_plan(step,again)||!preserved(current,guarded,TimingPlan{})||!transport_.unchanged(error)||!identity(api_,gpu_,target_)){
            result.message=stopped("Pre-commit timing baseline or helper changed");return result;}
        auto captured=expected;captured.value=expected.prior;undo.fields.push_back(std::move(captured));
        result.undo=undo;if(before_write)before_write(undo);
        // Entering commit is a dispatched attempt even if the transport
        // throws before it can report launch status. No later field enters Undo.
        result.may_have_changed=true;result.launched_fields.push_back(step.field);
        const auto write=transport_.run(TimingCommand::commit,target_.slot,step.field,step.value,c.commit,false);
        if(!write.launched){result.launched_fields.pop_back();result.may_have_changed=!result.launched_fields.empty();}
        if(transport_.pending()){result.message="Timing helper exit is unconfirmed; wait before selected restore";return result;}
        const auto after=capture_locked(api_,gpu_,target_,transport_);
        const auto checked=make_plan(after,step.field,step.value,transport_.token());
        if(!run_ok(write)||!preserved(guarded,after,step)||!same_field_plan(step,checked,false)){
            result.message="Timing transaction failed or selected/unselected bits changed; selected restore retained";return result;}
        current=after;
    }
    const auto final=make_batch(current,requests(plan),transport_.token());
    if(!final.ok||!final.no_change){result.message="Final selected timing requests differ; selected restore retained";return result;}
    result.ok=true;result.message="All selected timing fields and preserved register bits verified";return result;
    }catch(const std::exception& error){result.message=stopped(std::string("Timing transaction interrupted: ")+error.what());return result;}
    catch(...){result.message=stopped("Timing transaction interrupted");return result;}
}
TimingBatchResult TimingsService::apply(const TimingBatchPlan& plan,const std::function<void(const TimingBatchPlan&)>& before_write){
    auto result=apply_batch(plan,before_write);
    if(result.undo){auto& fields=result.undo->fields;
        fields.erase(std::remove_if(fields.begin(),fields.end(),[&](const auto& f){return std::find(result.launched_fields.begin(),result.launched_fields.end(),f.field)==result.launched_fields.end();}),fields.end());
        if(fields.empty())result.undo.reset();}
    return result;
}
TimingBatchResult TimingsService::restore(const TimingBatchPlan& undo,const std::function<void(const TimingBatchPlan&)>& before_write){
    TimingBatchResult result;
    auto fresh=preview(requests(undo));
    if(!fresh.ok||fresh.helper_token!=undo.helper_token||!same_band(undo.baseline,fresh.baseline)||fresh.fields.size()!=undo.fields.size()){
        result.message="Captured timing restore target, helper or band changed";return result;}
    for(size_t i=0;i<fresh.fields.size();++i)if(!same_field_plan(fresh.fields[i],undo.fields[i],false)){
        result.message="Captured timing restore field geometry changed";return result;}
    for(size_t i=0;i<fresh.fields.size();++i){const auto& saved=undo.fields[i];uint32_t shift=0;
        if(!saved.mask){result.message="Captured timing mask is invalid";return result;}
        while(!(saved.mask&(uint32_t{1}<<shift)))++shift;
        const uint32_t attempted=(saved.target_word&saved.mask)>>shift;
        if(fresh.fields[i].prior!=saved.value&&fresh.fields[i].prior!=attempted){result.message="Selected timing changed outside the recorded attempt; restore refused";return result;}}
    // Rebase only the selected values onto current unknown/unselected bits.
    return apply(fresh,before_write);
}
TimingBatchPlan TimingsService::preview_initial(){
    TimingBatchPlan invalid;if(!initial_){invalid.message="No qualified captured initial timings in this connection";return invalid;}
    const auto captured=*initial_;const auto token=initial_token_;
    std::vector<TimingRequest> values;
    for(const auto& f:captured.fields){const auto w=std::find_if(captured.broadcast.begin(),captured.broadcast.end(),[&](const auto& r){return r.name==f.reg;});
        bool overlap=false;for(const auto& other:captured.fields)if(&other!=&f&&other.reg==f.reg&&other.lo<=f.hi&&f.lo<=other.hi)overlap=true;
        if(!f.structural&&!f.inferred&&!f.overlapping&&!overlap&&w!=captured.broadcast.end()&&
            (w->confidence.empty()||w->confidence=="DOCUMENTED")&&f.hi<32&&f.lo<=f.hi)
            values.push_back({f.name,(w->value>>f.lo)&f.maximum});}
    auto p=preview(values);
    if(!p.ok||p.helper_token!=token||initial_token_!=token||!same_band(captured,p.baseline)){invalid.message="Captured initial timing helper or operating band changed";return invalid;}
    const auto original=make_batch(captured,values,token);
    if(!original.ok||original.fields.size()!=p.fields.size()){invalid.message="Captured initial timing fields unavailable";return invalid;}
    for(size_t i=0;i<p.fields.size();++i)if(!same_field_plan(original.fields[i],p.fields[i],false)){invalid.message="Captured initial timing geometry changed";return invalid;}
    p.message="Captured initial selected values ready; these are not factory defaults";return p;
}
TimingBatchResult TimingsService::restore_initial(const std::function<void(const TimingBatchPlan&)>& before_write){return apply(preview_initial(),before_write);}
}
