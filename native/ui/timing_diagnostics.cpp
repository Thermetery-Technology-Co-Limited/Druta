// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "timing_diagnostics.h"
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <ctime>
namespace druta::ui {
namespace {
const TimingWord* find_word(const std::vector<TimingWord>& words,const std::string& name){
    const TimingWord* result=nullptr;for(const auto& w:words)if(w.name==name){if(result)return nullptr;result=&w;}return result;
}
std::optional<uint32_t> extract(const TimingWord* w,const TimingField& f){
    if(!w||f.lo>f.hi||f.hi>=32)return {};
    const auto mask=(uint64_t{1}<<(f.hi-f.lo+1))-1;if(mask!=f.maximum)return {};
    return static_cast<uint32_t>((w->value>>f.lo)&mask);
}
// Released 1.9.1 decodes the `nvtune save` broadcast block, which carries
// registers (TIMING22) that `dump --raw` omits. The service files those words
// only when every word both reads share agrees, so a missing dump word may be
// taken from the save; a word present in the dump always wins.
const TimingWord* decode_word(const TimingSnapshot& s,const std::string& reg,bool released){
    const auto* w=find_word(s.broadcast,reg);
    if(!w&&released&&std::none_of(s.broadcast.begin(),s.broadcast.end(),[&](const TimingWord& x){return x.name==reg;}))w=find_word(s.saved_broadcast,reg);
    return w;
}
}
TimingDiagnostics timing_diagnostics(const TimingSnapshot& s,const TimingDiagnosticEvidence& e,bool released_ns_rule){
    TimingDiagnostics out;
    if(!s.ok)out.clock_status="No current capture";
    else if(!s.memory_before_mhz || !s.memory_after_mhz)out.clock_status="Memory clock unavailable";
    else if(!s.memory_clock_stable || *s.memory_before_mhz!=*s.memory_after_mhz)out.clock_status="Memory clock changed during capture";
    else out.clock_status=s.top_band?"Stable clock; top band established":"Stable clock; top band not established";
    out.partition_status=e.partitions.empty()?"Partition words not captured":e.partitions_stale?"Last partition capture is stale; absent fields are unknown":"Partition words captured; absent fields are unknown";
    for(const auto& f:s.fields){TimingDiagnosticRow row;row.field=f.name;row.reg=f.reg;
        if(s.ok)row.value=extract(decode_word(s,f.reg,released_ns_rule),f);
        const auto it=std::find_if(e.units.begin(),e.units.end(),[&](const TimingUnitEvidence& x){return x.field==f.name && x.reg==f.reg && x.codename==s.codename && x.lo==f.lo && x.hi==f.hi;});
        const bool qualified=row.value && !f.structural && (released_ns_rule || !f.inferred) && !f.overlapping && it!=e.units.end() && !it->provenance.empty();
        if(qualified && it->cycles){row.unit="cycles";row.provenance=it->provenance;
            if(it->nanoseconds && f.name!="RFC" && f.name!="WL" && s.memory_clock_stable && s.memory_before_mhz && s.memory_after_mhz &&
               *s.memory_before_mhz==*s.memory_after_mhz && *s.memory_before_mhz>0 && e.memory_clock_divisor &&
               std::isfinite(*e.memory_clock_divisor) && *e.memory_clock_divisor>0 && !e.divisor_provenance.empty())
                row.nanoseconds=*row.value*1000.0**e.memory_clock_divisor / *s.memory_before_mhz;
        }
        for(const auto& p:e.partitions){TimingPartitionValue v;v.scope=p.scope;
            if(s.ok)v.value=extract(find_word(p.words,f.reg),f);
            v.differs=row.value && v.value && *row.value!=*v.value;row.partitions.push_back(std::move(v));}
        out.rows.push_back(std::move(row));
    }return out;
}
std::vector<std::string> timing_split_siblings(const std::string& name,const std::vector<TimingField>& fields){
    static constexpr const char* suffixes[]{"_LO","_HI","_MSB","_LSB","_MID"};
    std::vector<std::string> names;for(const auto& f:fields)names.push_back(f.name);
    std::sort(names.begin(),names.end());names.erase(std::unique(names.begin(),names.end()),names.end());
    const auto known=[&](const std::string& n){return std::binary_search(names.begin(),names.end(),n);};
    std::vector<std::string> out;const auto add=[&](const std::string& n){if(std::find(out.begin(),out.end(),n)==out.end())out.push_back(n);};
    for(const char* sfx:suffixes)if(known(name+sfx))add(name+sfx);
    for(const char* sfx:suffixes){const std::string s(sfx);
        if(name.size()>s.size()&&name.compare(name.size()-s.size(),s.size(),s)==0){
            const auto head=name.substr(0,name.size()-s.size());
            for(const auto& n:names)if(n!=name&&(n==head||n.rfind(head+"_",0)==0))add(n);
            break;}}
    return out;
}
std::string timing_ns_refusal(const TimingSnapshot& s,const TimingField& f,const TimingDiagnosticRow& row){
    const auto present=[&](const std::vector<TimingWord>& words){return std::any_of(words.begin(),words.end(),[&](const TimingWord& w){return w.name==f.reg;});};
    if(!timing_layout_known(s.codename))return "unknown timing layout; raw registers only";
    if(!present(s.broadcast)&&!present(s.saved_broadcast))return "register "+f.reg+" not in snapshot";
    // Released NS_UNRELIABLE refuses RFC and WL by name; the reasons are this
    // build's own wording, without one card's measured figures.
    if(f.name=="RFC")return "RFC does not convert: a GDDR6 tRFC is expressed differently from this cycle count, or its range splits with another field.";
    if(f.name=="WL")return "GDDR6 write latency is expressed RELATIVE to CL, not as an absolute delay.";
    if(f.structural)return "structural - a fragment of a value split across bit ranges, not a delay of its own";
    if(const auto split=timing_split_siblings(f.name,s.fields);!split.empty()){
        std::string names;for(const auto& n:split)names+=(names.empty()?"":", ")+n;
        return "only part of the value - "+names+" hold(s) the rest, and the assembly rule is not verified";
    }
    if(!s.memory_before_mhz)return "no memory clock captured";
    const auto& div=s.diagnostic_evidence.memory_clock_divisor;
    if(!div||!std::isfinite(*div)||*div<=0||s.diagnostic_evidence.divisor_provenance.empty())return "memory type has no known true-clock divisor";
    if(!row.value)return "no captured value for this field";
    // Native only: a conversion needs this field's units established.
    return "no nanosecond conversion: this field's units are not established";
}
namespace {
std::string or_dash(const std::string& v){return v.empty()?std::string("--"):v;}
std::string format(const char* f,double v){char b[64];std::snprintf(b,sizeof(b),f,v);return b;}
std::optional<double> true_mhz(const TimingSnapshot& s){
    const auto& e=s.diagnostic_evidence;
    if(!s.memory_before_mhz||!*s.memory_before_mhz||!e.memory_clock_divisor||!std::isfinite(*e.memory_clock_divisor)||*e.memory_clock_divisor<=0||e.divisor_provenance.empty())return {};
    return *s.memory_before_mhz / *e.memory_clock_divisor;
}
// Released 1.9.1 in_performance_band for one bracketing read: nullopt when
// it cannot be placed. The band rule is the native capture's (top_band).
std::optional<bool> in_band(const TimingSnapshot& s,const std::optional<uint32_t>& memory,const std::optional<uint32_t>& pstate,const std::optional<int32_t>& offset){
    if(!pstate)return {};
    if(*pstate!=0&&*pstate!=2)return false;
    if(s.memory_states.empty()||!memory||!*memory||!offset)return {};
    return static_cast<double>(*memory)-*offset/2.0>=static_cast<double>(s.memory_states.back())-5.0;
}
std::optional<bool> perf_band(const TimingSnapshot& s){
    const auto a=in_band(s,s.memory_before_mhz,s.pstate_before,s.offset_before),b=in_band(s,s.memory_after_mhz,s.pstate_after,s.offset_after);
    if((a&&!*a)||(b&&!*b))return false;
    if(!a||!b)return {};
    return true;
}
std::string verdict(const std::vector<std::optional<uint32_t>>& cycles,const std::vector<std::optional<double>>& ratios,
                    const std::vector<double>& clock_ratios,bool structural,std::string& note){
    struct Pair{double measured,clock,base,other;};std::vector<Pair> pairs;
    for(size_t i=1;i<ratios.size()&&i<clock_ratios.size();++i)
        if(ratios[i]&&clock_ratios[i]!=0&&std::abs(clock_ratios[i]-1.0)>timing_flat_tolerance)
            pairs.push_back({*ratios[i],clock_ratios[i],static_cast<double>(*cycles[0]),static_cast<double>(*cycles[i])});
    if(pairs.empty()){note="no second memory state to compare against - these captures are at effectively the same clock";return "--";}
    if(std::all_of(pairs.begin(),pairs.end(),[](const Pair& p){return std::abs(p.measured-1.0)<=timing_flat_tolerance;})){
        note="unchanged across the clock states - a mode or bus-turnaround field, counted in cycles by design";return "flat";}
    if(std::all_of(pairs.begin(),pairs.end(),[](const Pair& p){return timing_cycles_agree(p.base,p.other,p.clock);})){
        note="moved by the memory-clock ratio - this field IS a cycle count of a fixed time, which is the decode being right";return "tracks";}
    const Pair* worst=&pairs.front();
    for(const auto& p:pairs)if(std::abs(p.measured-p.clock)/p.clock>std::abs(worst->measured-worst->clock)/worst->clock)worst=&p;
    note="moved, but "+format("%+.0f",(worst->measured-worst->clock)/worst->clock*100.0)+"% off the clock ratio";
    note+=structural?" - expected: this is a fragment of a split field"
        :" - the VBIOS also relaxes timings at low clocks, so this is not necessarily a bad decode; read the raw counts";
    return "partial";
}
}
TimingHeader timing_header(const TimingSnapshot& s){
    TimingHeader h;
    if(!s.ok){h.ident="no snapshot";h.clock=s.message.empty()?"nvtune has not been read yet":s.message;return h;}
    h.ident=or_dash(s.codename)+"  \u00b7  PCI "+or_dash(s.pci_id)+"  \u00b7  slot "+or_dash(s.slot)+"  \u00b7  FBPA aperture "+or_dash(s.aperture)+"  \u00b7  BOOT_0 "+or_dash(s.boot0);
    const auto mhz=true_mhz(s);
    h.clock="sampled at memory clock "+(s.memory_before_mhz?std::to_string(*s.memory_before_mhz):std::string("--"))+" reported";
    if(mhz)h.clock+="  =  "+format("%.0f",*mhz)+" MHz true ("+(s.memory_type.empty()?std::string("unknown"):s.memory_type)+")";
    if(s.captured_unix){const std::time_t t=static_cast<std::time_t>(*s.captured_unix);std::tm local{};
        if(!localtime_s(&local,&t)){char b[16];std::strftime(b,sizeof(b),"%H:%M:%S",&local);h.clock+=std::string("   \u00b7   ")+b;}}
    const auto& parts=s.diagnostic_evidence.partitions;
    h.clock+=parts.empty()?std::string("   \u00b7   partitions not captured")
        :"   \u00b7   "+std::to_string(parts.size())+" partitions"+(s.diagnostic_evidence.partitions_stale?" (earlier capture)":"");
    const bool stable=s.memory_clock_stable&&s.memory_before_mhz&&s.memory_after_mhz&&*s.memory_before_mhz==*s.memory_after_mhz;
    h.clock_trustworthy=timing_layout_known(s.codename)&&stable&&mhz.has_value();
    if(!timing_layout_known(s.codename))h.warnings.push_back("nvtune has no confirmed timing layout for "+(s.codename.empty()?std::string("this chip"):s.codename)+
        "; raw registers only, decoded timing fields and writes are unavailable");
    if(!s.memory_before_mhz)h.warnings.push_back("no memory clock was readable around this capture - every cycle count below is unanchored and the ns column is empty");
    else if(!stable)h.warnings.push_back("THE MEMORY CLOCK MOVED DURING CAPTURE: "+std::to_string(*s.memory_before_mhz)+" -> "+
        (s.memory_after_mhz?std::to_string(*s.memory_after_mhz):std::string("--"))+" (reported). These registers were read while the card was reclocking, "
        "so the ns column is arithmetic against a clock that no longer applies - treat the cycle counts as unanchored and take the capture again once the clock settles.");
    if(s.memory_before_mhz&&!mhz)h.warnings.push_back("memory type '"+(s.memory_type.empty()?std::string("unknown"):s.memory_type)+
        "' has no known true-clock divisor, so no cycle count here can be converted to nanoseconds");
    if(!s.offset_before||!s.offset_after)h.warnings.push_back("memory offset was not readable; the performance band is unverified");
    for(const auto& w:s.saved_broadcast.empty()?s.broadcast:s.saved_broadcast){char b[16];std::snprintf(b,sizeof(b),"0X%08X",w.value);h.words+=(h.words.empty()?"":"   ")+w.name+" "+b;}
    return h;
}
std::optional<bool> timing_perf_band(const TimingSnapshot& s){return perf_band(s);}
std::string timing_state_headline(const TimingSnapshot& s){
    const auto band=perf_band(s);
    if(!band)return "MEMORY STATE UNKNOWN: a memory clock, offset, performance state or supported clock list is unavailable, so this capture "
        "cannot be placed in a clock band. Treat it as unverified.";
    if(*band)return {};
    // Released 1.9.1 matched_state: the enumerated state under the reading
    // once the reported offset (half the raw request) is taken back off.
    std::string where="NVML "+(s.memory_before_mhz?std::to_string(*s.memory_before_mhz):std::string("--"));
    if(s.memory_before_mhz&&s.offset_before&&!s.memory_states.empty()){
        const double offset=*s.offset_before/2.0,base=static_cast<double>(*s.memory_before_mhz)-offset;
        const auto best=*std::min_element(s.memory_states.begin(),s.memory_states.end(),[&](uint32_t a,uint32_t b){return std::abs(a-base)<std::abs(b-base);});
        if(std::abs(best-base)<=5.0)where+=" (enumerated state "+std::to_string(best)+" + "+format("%.0f",offset)+" memory offset)";
    }
    std::optional<uint32_t> ps;for(const auto& p:{s.pstate_before,s.pstate_after})if(p&&(!ps||*p>*ps))ps=p;
    if(ps)where+=", P-STATE "+std::to_string(*ps);
    const bool stable=s.memory_clock_stable&&s.memory_before_mhz&&s.memory_after_mhz&&*s.memory_before_mhz==*s.memory_after_mhz;
    const std::string moved=stable?std::string():", and it moved to "+(s.memory_after_mhz?std::to_string(*s.memory_after_mhz):std::string("--"))+" mid-capture";
    // The band floor is the native capture's rule: the top enumerated state.
    // Released 1.9.1 names 'Induce P-state'; this build's load path is the
    // blue read's optional CUDA workload.
    return "IDLE STATE ("+where+")"+moved+" - says NOTHING about performance. Hold the card in its top band with Ctrl+H on the V/F curve, or "
        "capture under load with 'Read memory timings  (will hold P0)', in P0/P2 at or above nominal "+(s.memory_states.empty()?std::string("--"):std::to_string(s.memory_states.back()))+
        " MHz after removing the memory offset.";
}
std::string timing_state_tag(const TimingSnapshot& s){
    const auto band=perf_band(s);if(!band)return "?";
    std::optional<uint32_t> ps;for(const auto& p:{s.pstate_before,s.pstate_after})if(p&&(!ps||*p>*ps))ps=p;
    if(*band&&s.pstate_before&&s.pstate_after&&*s.pstate_before==0&&*s.pstate_after==0)return "P0";
    if(*band)return ps?"P"+std::to_string(*ps)+" band":std::string("band");
    return "idle";
}
std::optional<TimingCapture> timing_capture_for_comparison(const TimingSnapshot& s){
    if(!s.ok||!s.memory_clock_stable||!s.memory_before_mhz||!*s.memory_before_mhz||!s.memory_after_mhz||*s.memory_after_mhz!=*s.memory_before_mhz)return {};
    TimingCapture c;c.memory_mhz=*s.memory_before_mhz;c.true_mhz=true_mhz(s);c.top_band=s.top_band;c.tag=timing_state_tag(s);
    for(const auto& p:{s.pstate_before,s.pstate_after})if(p&&(!c.pstate||*p>*c.pstate))c.pstate=p;
    const bool known=timing_layout_known(s.codename);
    for(const auto& f:s.fields){TimingCaptureField x;x.name=f.name;x.reg=f.reg;x.bits="["+std::to_string(f.hi)+":"+std::to_string(f.lo)+"]";x.structural=f.structural;
        if(known)x.cycles=extract(decode_word(s,f.reg,true),f);c.fields.push_back(std::move(x));}
    return c;
}
bool timing_cycles_agree(double base,double other,double clock_ratio){
    const double predicted=base*clock_ratio;
    const double tolerance=clock_ratio+1.0+timing_ratio_tolerance*std::abs(predicted);
    return std::abs(other-predicted)<=tolerance;
}
TimingComparison compare_timing_captures(const std::vector<TimingCapture>& captures){
    TimingComparison out;
    for(size_t i=0;i<captures.size();++i)if(captures[i].memory_mhz)out.order.push_back(i);
    std::stable_sort(out.order.begin(),out.order.end(),[&](size_t a,size_t b){return captures[a].memory_mhz<captures[b].memory_mhz;});
    if(out.order.size()<2)return out;
    const auto& base=captures[out.order.front()];
    for(const auto i:out.order)out.clock_ratios.push_back(captures[i].memory_mhz/static_cast<double>(base.memory_mhz));
    for(const auto& f:base.fields){TimingComparisonRow row;row.name=f.name;row.reg=f.reg;row.bits=f.bits;
        for(const auto i:out.order){const auto& fs=captures[i].fields;
            const auto it=std::find_if(fs.begin(),fs.end(),[&](const TimingCaptureField& x){return x.name==f.name;});
            row.cycles.push_back(it==fs.end()?std::nullopt:it->cycles);}
        const auto b=row.cycles.front();
        for(const auto& v:row.cycles)row.ratios.push_back(b&&*b&&v?std::optional<double>(*v/static_cast<double>(*b)):std::nullopt);
        row.verdict=verdict(row.cycles,row.ratios,out.clock_ratios,f.structural,row.note);
        out.rows.push_back(std::move(row));}
    return out;
}
}
