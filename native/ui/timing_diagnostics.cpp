// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "timing_diagnostics.h"
#include <algorithm>
#include <cmath>
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
}
TimingDiagnostics timing_diagnostics(const TimingSnapshot& s,const TimingDiagnosticEvidence& e){
    TimingDiagnostics out;
    if(!s.ok)out.clock_status="No current capture";
    else if(!s.memory_before_mhz || !s.memory_after_mhz)out.clock_status="Memory clock unavailable";
    else if(!s.memory_clock_stable || *s.memory_before_mhz!=*s.memory_after_mhz)out.clock_status="Memory clock changed during capture";
    else out.clock_status=s.top_band?"Stable clock; top band established":"Stable clock; top band not established";
    out.partition_status=e.partitions.empty()?"Partition words not captured":e.partitions_stale?"Last partition capture is stale; absent fields are unknown":"Partition words captured; absent fields are unknown";
    for(const auto& f:s.fields){TimingDiagnosticRow row;row.field=f.name;row.reg=f.reg;
        if(s.ok)row.value=extract(find_word(s.broadcast,f.reg),f);
        const auto it=std::find_if(e.units.begin(),e.units.end(),[&](const TimingUnitEvidence& x){return x.field==f.name && x.reg==f.reg && x.codename==s.codename && x.lo==f.lo && x.hi==f.hi;});
        const bool qualified=row.value && !f.structural && !f.inferred && !f.overlapping && it!=e.units.end() && !it->provenance.empty();
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
}
