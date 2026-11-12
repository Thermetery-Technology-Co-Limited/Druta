/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "timing_profile.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <limits>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do { ++checks; if (!(x)) { std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x); std::abort(); } } while(false)
static dc_gpu_identity target() {
    dc_gpu_identity id{}; id.nvapi_ok=id.nvapi_selected=1;
    std::strcpy(id.slot,"0000:09:00.0"); std::strcpy(id.uuid,"GPU-alternate"); return id;
}
static TimingSnapshot snapshot() {
    TimingSnapshot s; s.ok=s.top_band=s.memory_clock_stable=true; s.slot="00000000:09:00.0"; s.codename="AD102";
    s.memory_before_mhz=s.memory_after_mhz=10000; s.pstate_before=s.pstate_after=0; s.offset_before=s.offset_after=0;
    s.broadcast={{"REG_A","DOCUMENTED",0x100,0xa5a51234},{"REG_B","",0x108,0x87654321}};
    s.fields={{"tA","REG_A","",0,3,15},{"tB","REG_A","",8,15,255},{"tC","REG_B","",16,23,255}};
    return s;
}
static bool rejected(const ProfileJson& p,const dc_gpu_identity& id,const TimingSnapshot& s) {
    try { (void)validate_timing_profile(p,id,s); } catch(const std::runtime_error&) { return true; } return false;
}
int main() {
    const auto id=target(); auto s=snapshot(); const std::vector<TimingProfileRequest> request={{"tA",5},{"tB",77},{"tC",0}};
    const auto doc=capture_timing_profile(id,s,request); const auto decoded=validate_timing_profile(doc,id,s);
    CHECK(decoded.size()==3 && decoded[0].field=="tA" && decoded[0].value==5 && decoded[1].value==77 && decoded[2].value==0);
    CHECK(s.broadcast[0].value==0xa5a51234 && !doc.contains("registers")); // never serialize raw words
    auto bad=doc; bad["target"]["uuid"]="GPU-other"; CHECK(rejected(bad,id,s));
    bad=doc; bad["target"]["slot"]="0000:10:00.0"; CHECK(rejected(bad,id,s));
    bad=doc; bad["codename"]="OTHER"; CHECK(rejected(bad,id,s));
    bad=doc; bad["band"]["memory_mhz"]=9999; CHECK(rejected(bad,id,s));
    bad=doc; bad["band"]["memory_offset"]=1; CHECK(rejected(bad,id,s));
    bad=doc; bad["fields"][2]["offset"]=0x10c; CHECK(rejected(bad,id,s));
    bad=doc; bad["fields"][2]["lo"]=15; CHECK(rejected(bad,id,s));
    bad=doc; bad["fields"][2]["value"]=256; CHECK(rejected(bad,id,s));
    bad=doc; bad["fields"][2]["value"]=true; CHECK(rejected(bad,id,s));
    bad=doc; bad["fields"][2]["value"]=1.5; CHECK(rejected(bad,id,s));
    bad=doc; bad["fields"][2]["value"]=-1; CHECK(rejected(bad,id,s));
    bad=doc; bad["fields"][2]["field"]="unknown"; CHECK(rejected(bad,id,s));
    bad=doc; bad["fields"][2]=bad["fields"][0]; CHECK(rejected(bad,id,s));
    bad=doc; bad["fields"][2]["extra"]=1; CHECK(rejected(bad,id,s));
    bad=doc; bad["version"]=2; CHECK(rejected(bad,id,s));
    bad=doc; bad["fields"]=ProfileJson::array(); CHECK(rejected(bad,id,s));
    auto changed=s; changed.fields[2].inferred=true; CHECK(rejected(doc,id,changed));
    changed=s; changed.fields[2].structural=true; CHECK(rejected(doc,id,changed));
    changed=s; changed.fields[2].overlapping=true; CHECK(rejected(doc,id,changed));
    changed=s; changed.fields.push_back({"hidden","REG_A","",2,6,31}); CHECK(rejected(doc,id,changed));
    changed=s; changed.fields.push_back(changed.fields[0]); CHECK(rejected(doc,id,changed));
    changed=s; changed.broadcast[0].confidence="INFERRED"; CHECK(rejected(doc,id,changed));
    changed=s; changed.broadcast[1].offset=0x100; CHECK(rejected(doc,id,changed));
    changed=s; changed.memory_after_mhz=9999; CHECK(rejected(doc,id,changed));
    changed=s; changed.top_band=false; CHECK(rejected(doc,id,changed));
    changed=s; changed.offset_after.reset(); CHECK(rejected(doc,id,changed));
    changed=s; changed.codename="UNKNOWN"; CHECK(rejected(doc,id,changed));
    auto bad_id=id; std::strcpy(bad_id.slot,"0000:10:00.0"); CHECK(rejected(doc,bad_id,s));
    bad_id=id; bad_id.pairing_error[0]='x'; CHECK(rejected(doc,bad_id,s));
    bool capture_rejected=false; try { (void)capture_timing_profile(id,s,{{"tA",2},{"tA",2}}); } catch(const std::runtime_error&) {capture_rejected=true;}
    CHECK(capture_rejected && s.broadcast[0].value==0xa5a51234);
    std::printf("timing_profile_test: %u checks passed\n",checks);
}
