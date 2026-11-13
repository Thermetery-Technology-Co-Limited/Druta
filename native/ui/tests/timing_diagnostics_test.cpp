// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "timing_diagnostics.h"
#include <cmath>
#include <cstdio>
using namespace druta::ui;
int failures=0;
void check(bool b,const char* s){if(!b){++failures;std::printf("FAIL: %s\n",s);}}
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
    s.memory_after_mhz=4000;e.memory_clock_divisor.reset();check(!timing_diagnostics(s,e).rows[0].nanoseconds,"missing divisor is not zero or inferred");
    s.ok=false;check(!timing_diagnostics(s,e).rows[0].value,"failed capture does not publish stale as current");
    std::printf("Timing diagnostics: %d failures\n",failures);return failures?1:0;
}
