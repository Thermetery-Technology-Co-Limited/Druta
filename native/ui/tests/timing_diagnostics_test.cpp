// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "timing_diagnostics.h"
#include <cmath>
#include <cstdio>
#include <initializer_list>
using namespace druta::ui;
int failures=0;
void check(bool b,const char* s){if(!b){++failures;std::printf("FAIL: %s\n",s);}}
// Released 1.9.1 _split_siblings and Reading.ns_refusal.
void split_and_refusals(){
    const auto named=[](std::initializer_list<const char*> names){std::vector<TimingField> out;for(const auto* n:names){TimingField f;f.name=n;f.reg="CONFIG0";out.push_back(f);}return out;};
    const auto fields=named({"REFRESH","REFRESH_LO","DELAY0","DELAY0_MSB","DELAY0_HI","WR","WR_RCD","RC"});
    using V=std::vector<std::string>;
    check(timing_split_siblings("REFRESH",fields)==V{"REFRESH_LO"},"a head names its fragment");
    check(timing_split_siblings("REFRESH_LO",fields)==V{"REFRESH"},"a fragment names its head");
    check(timing_split_siblings("DELAY0",fields)==V{"DELAY0_HI","DELAY0_MSB"},"suffix order: _LO, _HI, _MSB, _LSB, _MID");
    check(timing_split_siblings("DELAY0_MSB",fields)==V{"DELAY0","DELAY0_HI"},"a fragment names its head and the other fragments, sorted");
    check(timing_split_siblings("WR",fields).empty()&&timing_split_siblings("WR_RCD",fields).empty()&&timing_split_siblings("RC",fields).empty(),
        "unrelated names with a shared prefix are not one split value");
    TimingSnapshot s;s.ok=true;s.codename="GA102";s.memory_before_mhz=s.memory_after_mhz=10501;s.memory_clock_stable=true;
    s.diagnostic_evidence.memory_clock_divisor=8;s.diagnostic_evidence.divisor_provenance="adapter";
    s.fields=fields;s.fields[1].structural=true;TimingField rfc;rfc.name="RFC";rfc.reg="CONFIG0";s.fields.push_back(rfc);
    TimingField t22;t22.name="RFCSB";t22.reg="TIMING22";s.fields.push_back(t22);
    s.broadcast={{"CONFIG0","DOCUMENTED",0x1000,0}};
    TimingDiagnosticRow valued;valued.value=7;
    const auto refusal=[&](size_t i,const TimingDiagnosticRow& r=TimingDiagnosticRow{}){return timing_ns_refusal(s,s.fields[i],r);};
    check(refusal(1)=="structural - a fragment of a value split across bit ranges, not a delay of its own","structural before split");
    check(refusal(0)=="only part of the value - REFRESH_LO hold(s) the rest, and the assembly rule is not verified","split head names the fragment");
    check(refusal(8).rfind("RFC does not convert",0)==0,"RFC refuses by name, first");
    check(refusal(9)=="register TIMING22 not in snapshot","a register the capture lacks is named");
    s.saved_broadcast={{"TIMING22","",0,0}};check(refusal(9)!="register TIMING22 not in snapshot","the agreeing save words supply the register");
    check(refusal(7,valued)=="no nanosecond conversion: this field's units are not established","native: units must be established");
    check(refusal(7)=="no captured value for this field","no value");
    s.diagnostic_evidence.memory_clock_divisor.reset();check(refusal(7,valued)=="memory type has no known true-clock divisor","no divisor");
    s.memory_before_mhz.reset();check(refusal(7,valued)=="no memory clock captured","no clock");
    s.codename="GENERIC";check(refusal(1)=="unknown timing layout; raw registers only","unknown layout first");
}
int main(){
    TimingSnapshot s;s.ok=true;s.codename="TU104";s.memory_before_mhz=4000;s.memory_after_mhz=4000;s.memory_clock_stable=true;s.top_band=true;
    TimingField f;f.name="RC";f.reg="CONFIG0";f.lo=4;f.hi=11;f.maximum=255;s.fields.push_back(f);
    s.broadcast.push_back({"CONFIG0","known",0,20u<<4});
    auto raw=timing_diagnostics(s);check(raw.rows[0].value==20 && raw.rows[0].unit=="raw" && !raw.rows[0].nanoseconds,"no unit inferred from field name");
    TimingDiagnosticEvidence e;e.units.push_back({"RC","CONFIG0","TU104",4,11,true,true,"helper-qualified cycles"});
    e.memory_clock_divisor=4;e.divisor_provenance="current memory type";
    e.partitions={{"partition 0",{{"CONFIG0","known",0,20u<<4}}},{"partition 1",{{"CONFIG0","known",0,21u<<4}}},{"partition 2",{}}};
    auto d=timing_diagnostics(s,e);check(d.rows[0].unit=="cycles" && d.rows[0].nanoseconds && std::abs(*d.rows[0].nanoseconds-20)<1e-9,"qualified units and clock conversion");
    check(!d.rows[0].partitions[0].differs && d.rows[0].partitions[1].differs && !d.rows[0].partitions[2].value,"partial partitions distinguish absent and divergence");
    s.codename="GA104";check(timing_diagnostics(s,e).rows[0].unit=="raw","evidence bound to codename");s.codename="TU104";
    s.fields[0].lo=5;check(timing_diagnostics(s,e).rows[0].unit=="raw","changed geometry refuses old evidence");s.fields[0]=f;
    s.memory_after_mhz=3900;check(!timing_diagnostics(s,e).rows[0].nanoseconds,"reclock suppresses nanoseconds even if stale flag says stable");
    s.memory_after_mhz=4000;
    {   // Focused (the native rule) keeps an INFERRED field raw although the capture now carries its
        // unit evidence; only Classic's released rule converts it. A word only in the save output
        // is decoded by the released rule alone.
        auto inferred=s;inferred.fields[0].inferred=true;
        const auto focused=timing_diagnostics(inferred,e);
        check(focused.rows[0].value==20&&focused.rows[0].unit=="raw"&&focused.rows[0].provenance.empty()&&!focused.rows[0].nanoseconds,"Focused gives an inferred field no cycles or ns");
        const auto released=timing_diagnostics(inferred,e,true);
        check(released.rows[0].unit=="cycles"&&released.rows[0].nanoseconds,"Classic's released rule converts the inferred field");
        auto saved=s;saved.saved_broadcast=saved.broadcast;saved.broadcast.clear();
        check(!timing_diagnostics(saved,e).rows[0].value&&timing_diagnostics(saved,e,true).rows[0].value==20,"Focused ignores the save-output words");
    }
    e.memory_clock_divisor.reset();check(!timing_diagnostics(s,e).rows[0].nanoseconds,"missing divisor is not zero or inferred");
    s.ok=false;check(!timing_diagnostics(s,e).rows[0].value,"failed capture does not publish stale as current");
    split_and_refusals();
    std::printf("Timing diagnostics: %d failures\n",failures);return failures?1:0;
}
