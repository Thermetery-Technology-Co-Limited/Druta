// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_tables_fake.h"
using namespace druta::ui;
using namespace voltage_tables_test;
namespace {
int failures=0;
void check(bool ok,const char* why){if(!ok){++failures;std::printf("FAIL: %s\n",why);}}
template<class F>bool rejects(F action){try{action();return false;}catch(const std::exception&){return true;}}
void normal(){Fake x;VoltageTablesService service(api(),x.gpu(),x.id);check(x.reads==0&&x.sets==0,"construction does not inspect or apply");
    const auto& read=service.read(1);check(read.requests_complete&&read.rows.size()==12&&read.gate_enabled==true,"complete independent request inventory including zero control discriminator");
    const auto before=x.public_rows;auto r=service.apply(request(service,0,4,-123456));
    check(r.ok&&r.may_have_changed&&x.value(0,4)==-123456&&x.sets==1,"selected signed VCRIT request writes through exact public V2 setter");
    auto expected=before;put(expected[0],0x48+4+4*4,static_cast<uint32_t>(-123456));check(expected==x.public_rows&&x.native[8]==17,"all five peers, other rail, boost and unknown returned bytes preserved");
    check(service.undo().ok&&x.public_rows==before&&!service.state().undo_available,"selected Undo returns previous request, no opaque packet replay");
    check(service.apply(request(service,1,5,65000)).ok,"other rail request independently writable");
    check(service.restore_initial({VoltageTableFamily::requests,1,5}).ok&&x.value(1,5)==2555,"captured initial is an explicit selected value operation");
    check(x.depth==0,"GPU lock released after all operations");
}
void qualification(){Fake x;x.info[12+0x33]=5;x.info[12+76+0x33]=8;VoltageTablesService service(api(),x.gpu(),x.id);
    check(service.read(1).requests_complete,"alternate independently paired rail identities qualify");
    x.admission=false;service.read(1);const int sets=x.sets;auto r=service.apply(request(service,0,0,2000));
    check(!r.ok&&x.sets==sets&&!x.admission,"disabled gate never silently enabled by request Apply");
    x.admission=true;x.public_present=1;service.read(1);check(!service.state().requests_complete&&service.state().rows[0].value.has_value()&&!service.state().rows[0].writable,"partial peer capability keeps readable rail but blocks complete SET");
    check(!service.apply(request(service,0,0,2000)).ok&&x.sets==sets,"partial preservation never fabricates absent peer values");
    Fake layout;put(layout.public_rows[0],0,0x10AC8);VoltageTablesService ls(api(),layout.gpu(),layout.id);
    check(!ls.read(1).requests_complete&&!ls.state().rows[0].writable,"V1 getter fallback remains unqualified for exact V2 request replay");
    Fake arch;arch.arch=3;VoltageTablesService as(api(),arch.gpu(),arch.id);as.read(1);check(as.state().rows.empty()&&arch.sets==0,"architectural gate precedes table access");
    Fake malformed;malformed.info[12]=99;VoltageTablesService ms(api(),malformed.gpu(),malformed.id);check(!ms.read(1).requests_complete,"unknown native/public control mapping isolates row");
    Fake identity;VoltageTablesService is(api(),identity.gpu(),identity.id);std::snprintf(identity.id.uuid,sizeof(identity.id.uuid),"GPU-other");is.read(1);check(identity.reads==0&&identity.sets==0,"changed UUID refuses packet access");
    Fake transient;transient.fail_reads=1;VoltageTablesService ts(api(),transient.gpu(),transient.id);check(ts.read(1).requests_complete&&transient.refreshes>0,"bounded transient read-only recovery");
    transient.fail_reads=100;ts.read(1);check(ts.state().rows.size()==12&&ts.state().rows[0].value&&ts.state().rows[0].stale&&!ts.state().rows[0].writable,"last-good values retained stale, never converted to unsupported");
    check(!ts.state().offsets_message.empty()&&ts.state().offsets_message.find("receipt")!=std::string::npos,"scaffold offset dependency remains explicit rather than generic constant substitution");
}
void recovery(){Fake x;VoltageTablesService s(api(),x.gpu(),x.id);s.read(1);x.post_set_failures=100;
    auto r=s.apply(request(s,0,0,5000));check(!r.ok&&r.may_have_changed&&s.state().recovery_pending&&x.sets==1,"accepted SET with failed capture retains selected recovery, never retries requested write");
    x.fail_reads=0;check(s.recover().ok&&x.value(0,0)==1000&&!s.state().recovery_pending,"later explicit selected recovery rebases fresh complete state");
    Fake failed;VoltageTablesService fs(api(),failed.gpu(),failed.id);fs.read(1);failed.set_error=true;r=fs.apply(request(fs,0,0,5000));
    check(!r.ok&&r.may_have_changed&&fs.state().recovery_pending,"failed return after accepted SET and restoration keeps uncertain Undo");
    failed.set_error=false;check(fs.recover().ok&&failed.value(0,0)==1000,"already-restored selected request verified without blind SET");
    Fake foreign;VoltageTablesService cs(api(),foreign.gpu(),foreign.id);cs.read(1);foreign.foreign_after_set=true;r=cs.apply(request(cs,0,0,5000));
    check(!r.ok&&cs.state().recovery_pending&&foreign.sets==1&&foreign.value(0,0)==987654,"unrecognized selected replacement is never overwritten");
    Fake unknown;VoltageTablesService us(api(),unknown.gpu(),unknown.id);us.read(1);unknown.unknown_after_set=true;r=us.apply(request(us,0,0,5000));
    check(!r.ok&&r.rollback_verified&&unknown.value(0,0)==1000&&unknown.public_rows[1][0x3C]==0x12,"exact metadata mismatch triggers selected-only compensation preserving changed unknown field");
    Fake replaced;VoltageTablesService rs(api(),replaced.gpu(),replaced.id);rs.read(1);replaced.identity_after_set=true;r=rs.apply(request(rs,0,0,5000));
    check(!r.ok&&rs.state().recovery_pending&&replaced.sets==1,"target change after dispatch cannot authorize compensation");
}
void profiles(){Fake x;VoltageTablesService s(api(),x.gpu(),x.id);s.read(1);
    const std::vector<VoltageTableKey> keys={{VoltageTableFamily::requests,0,1},{VoltageTableFamily::requests,1,5}};
    auto profile=capture_voltage_tables_profile(s.state(),keys);auto staged=stage_voltage_tables_profile(profile,s.state());
    check(staged.size()==2&&x.sets==0&&profile.dump().find("packet")==std::string::npos,"manual profile contains selected typed fields, never packet replay");
    profile["fields"][0]["value_uv"]=-3000;profile["fields"][1]["value_uv"]=50000;staged=stage_voltage_tables_profile(profile,s.state());
    check(s.apply(staged).ok&&x.value(0,1)==-3000&&x.value(1,5)==50000,"explicit staged multi-field profile preflights all selected identities");
    check(s.undo().ok&&x.value(0,1)==1111&&x.value(1,5)==2555,"one Undo restores complete selected profile group");
    auto invalid=profile;invalid["fields"][1]["control_type"]=3;check(rejects([&]{stage_voltage_tables_profile(invalid,s.state());})&&x.sets==4,"later invalid field rejects entire staged profile before write");
    invalid=profile;invalid["target"]["uuid"]="GPU-other";check(rejects([&]{stage_voltage_tables_profile(invalid,s.state());}),"manual profile UUID mismatch rejected");
    auto stale=request(s,0,0,5000);x.set(0,0,1001);check(!s.apply(stale).ok&&x.sets==4,"stale selected draft does not silently rebase");
    x.admission=false;s.read(1);const auto staged_gate_off=stage_voltage_tables_profile(profile,s.state());
    check(staged_gate_off.size()==2&&!s.apply(staged_gate_off).ok&&x.sets==4&&!x.admission,"gate-off Load and staging are read-only; fresh Apply still enforces admission independently");
    Fake context;VoltageTablesService contextual(api(),context.gpu(),context.id);contextual.read(1);
    check(contextual.apply(request(contextual,0,0,5000)).ok,"binding-context initial fixture writes qualified selected request");
    context.info[12+0x33]=5;contextual.read(1);const int context_sets=context.sets;
    check(contextual.state().rows[0].initial==5000&&contextual.restore_initial({VoltageTableFamily::requests,0,0}).ok
        &&context.value(0,0)==5000&&context.sets==context_sets,"changed policy/INFO binding captures its own initial; Restore cannot replay old layout initial");
}
void nvapi_only(){Fake x;x.id.uuid[0]=0;x.id.nvml_ok=x.id.nvml_selected=0;VoltageTablesService s(api(),x.gpu(),x.id);
    check(s.read(1).requests_complete&&s.state().rows[0].writable,"runtime tables qualify complete NVAPI-only slot/handle pairing without a UUID");
    const auto edit=request(s,0,0,5000);check(s.apply(edit).ok&&s.undo().ok&&x.value(0,0)==1000,"NVAPI-only runtime selected Apply and Undo remain qualified");
    check(rejects([&]{capture_voltage_tables_profile(s.state(),{{VoltageTableFamily::requests,0,0}});}),"saved profiles still require UUID identity");
    const auto next=request(s,0,0,6000);const int before=x.sets;std::snprintf(x.id.uuid,sizeof(x.id.uuid),"GPU-appeared");
    check(!s.apply(next).ok&&x.sets==before,"UUID appearance changes runtime binding and refuses SET");
    Fake mismatch;VoltageTablesService ms(api(),mismatch.gpu(),mismatch.id);ms.read(1);const auto saved=request(ms,0,0,5000);
    std::snprintf(mismatch.id.uuid,sizeof(mismatch.id.uuid),"GPU-other");check(!ms.apply(saved).ok&&mismatch.sets==0,"nonempty UUID mismatch refuses runtime table write");
}
}
int main(){normal();qualification();recovery();profiles();nvapi_only();std::printf("Voltage tables service: %d failures\n",failures);return failures?1:0;}
