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
    s.helper_token="current-helper-token"; s.memory_before_mhz=s.memory_after_mhz=10000; s.pstate_before=s.pstate_after=0; s.offset_before=s.offset_after=0;
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
    const auto editor=capture_timing_editor_profile(id,s,{{"tB",99}});
    const auto editor_fields=validate_timing_profile(editor,id,s);
    CHECK(editor_fields.size()==3&&editor_fields[0].field=="tA"&&editor_fields[0].value==4);
    CHECK(editor_fields[1].field=="tB"&&editor_fields[1].value==99&&editor_fields[2].value==0x65);
    CHECK(!editor.contains("registers"));
    auto current_only=capture_timing_editor_profile(id,s,{});CHECK(validate_timing_profile(current_only,id,s).size()==3);
    bool duplicate_staged=false;try{capture_timing_editor_profile(id,s,{{"tA",1},{"tA",2}});}catch(const std::runtime_error&){duplicate_staged=true;}CHECK(duplicate_staged);
    s.aliases={{"t0","tB"}};
    ProfileJson flat={{"fields",{{"tA",5},{"t0",77}}}};
    CHECK(is_legacy_timing_profile(flat));auto flat_fields=validate_timing_profile(flat,id,s);
    CHECK(flat_fields.size()==2&&flat_fields[0].field=="tB"&&flat_fields[0].value==77&&flat_fields[1].field=="tA");
    const auto normalized=normalize_timing_profile(flat,id,s);CHECK(normalized["format"]=="druta-native-timing"&&normalized["target"]["uuid"]=="GPU-alternate");
    ProfileJson python={{"schema","druta.timing-profile"},{"version",1},{"capture",{{"source","nvtune decoded broadcast capture"},{"gpu",{{"slot","0000:09:00.0"},{"codename","AD102"}}},{"band",{{"memory_mhz_reported",10000},{"performance_band",true},{"pstate",0},{"matched_memory_state_mhz",10000}}},{"pending_modifications",ProfileJson::array()}}},{"fields",{{"tA",6},{"tB",55}}}};
    CHECK(validate_timing_profile(python,id,s).size()==2);
    ProfileJson upstream={{"_format","nvtune-profile-1"},{"name","saved"},{"slot","0000:09:00.0"},{"fields",{{"TA",3}}}};
    auto upper=s;upper.fields[0].name="TA";CHECK(validate_timing_profile(upstream,id,upper)[0].field=="TA");
    auto mixed=flat;mixed["scope"]="unknown";mixed["nvvdd_offset_mv"]=50;
    CHECK(!is_legacy_timing_profile(mixed)&&rejected(mixed,id,s));
    mixed=flat;mixed["power_limit_mw"]=250000;CHECK(!is_legacy_timing_profile(mixed)&&rejected(mixed,id,s));
    mixed=flat;mixed["version"]=1;CHECK(!is_legacy_timing_profile(mixed)&&rejected(mixed,id,s));
    mixed=python;mixed["capture"]["gpu"]["set_voltage_mv"]=1000;CHECK(!is_legacy_timing_profile(mixed)&&rejected(mixed,id,s));
    mixed=python;mixed["capture"]["band"]["clock_offset"]=5;CHECK(!is_legacy_timing_profile(mixed)&&rejected(mixed,id,s));
    mixed=upstream;mixed["schema"]="druta.timing-profile";CHECK(!is_legacy_timing_profile(mixed)&&rejected(mixed,id,s));
    auto described=flat;described["name"]="Memory profile";described["description"]="informational";described["slot"]="0000:09:00.0";
    CHECK(is_legacy_timing_profile(described)&&validate_timing_profile(described,id,s).size()==2);
    auto invalid_legacy=flat;invalid_legacy["fields"]["tB"]=77;CHECK(rejected(invalid_legacy,id,s));
    invalid_legacy=flat;invalid_legacy["fields"]["T0"]=5;CHECK(rejected(invalid_legacy,id,s));
    invalid_legacy=flat;invalid_legacy["fields"]["tA"]=true;CHECK(rejected(invalid_legacy,id,s));
    invalid_legacy=flat;invalid_legacy["fields"]["tA"]=16;CHECK(rejected(invalid_legacy,id,s));
    invalid_legacy=flat;invalid_legacy["registers"]=ProfileJson::object();CHECK(rejected(invalid_legacy,id,s));
    invalid_legacy=flat;invalid_legacy["fbpa"]=ProfileJson::object();CHECK(rejected(invalid_legacy,id,s));
    invalid_legacy=python;invalid_legacy["capture"]["gpu"]["slot"]="0000:08:00.0";CHECK(rejected(invalid_legacy,id,s));
    invalid_legacy=python;invalid_legacy["capture"]["gpu"]["codename"]="GA102";CHECK(rejected(invalid_legacy,id,s));
    invalid_legacy=python;invalid_legacy["capture"]["band"]["memory_mhz_reported"]=9000;CHECK(rejected(invalid_legacy,id,s));
    invalid_legacy=python;invalid_legacy["capture"]["band"]["performance_band"]=false;CHECK(rejected(invalid_legacy,id,s));
    invalid_legacy=upstream;invalid_legacy["fields"]={{"tA",3}};CHECK(rejected(invalid_legacy,id,s));
    auto bad_alias=s;bad_alias.aliases.push_back({"T0","tA"});CHECK(rejected(flat,id,bad_alias));
    bad_alias=s;bad_alias.aliases={{"t0","missing"}};CHECK(rejected(flat,id,bad_alias));
    auto no_helper=s;no_helper.helper_token.clear();CHECK(rejected(flat,id,no_helper));
    auto unsafe=s;unsafe.fields[1].structural=true;CHECK(rejected(flat,id,unsafe));
    unsafe=s;unsafe.fields[1].inferred=true;CHECK(rejected(flat,id,unsafe));
    unsafe=s;unsafe.fields.push_back({"overlap","REG_A","",8,10,7});CHECK(rejected(flat,id,unsafe));
    CHECK(s.broadcast[0].value==0xa5a51234); // stage-only normalization
    std::printf("timing_profile_test: %u checks passed\n",checks);
}
