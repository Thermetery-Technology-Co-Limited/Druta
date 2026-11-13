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

ProfileJson python_requests(const VoltageTablesData& data){const auto& t=*data.target;return {{"schema",2},{"scope","voltage_requests"},{"saved_at","2026-11-13"},{"saved_ts",1.5},{"device",{{"name","alternate GPU"},{"uuid",data.identity.uuid},{"slot",data.identity.slot},{"driver",nullptr},{"vbios",nullptr}}},{"incomplete",ProfileJson::array()},{"voltage_requests",{{"identity",{{"slot",data.identity.slot},{"architecture",t.architecture},{"device_id",t.device_id},{"subsystem_id",t.subsystem_id}}},{"rails",{{"0",{{"identity",{{"name","NVVDD"},{"policy_type",1},{"control_type",0}}},{"offsets_uv",{{"REL",INT32_MIN}}}}},{"1",{{"identity",{{"name","MSVDD"},{"policy_type",3},{"control_type",2}}},{"offsets_uv",{{"ALT/OP",INT32_MAX},{"VCRIT high",1234}}}}}}}}}};}
void exchange_profiles(){Fake x;std::snprintf(x.id.uuid,sizeof(x.id.uuid),"GPU-alternate-profile-import");x.id.nvapi_devid=0x4321;x.id.nvapi_subsys=0xBADFACE;VoltageTablesService s(api(),x.gpu(),x.id);s.read(1);const auto data=s.state();const auto document=python_requests(data);const auto reads=x.reads;
    const auto description=describe_voltage_tables_profile(document);check(description.slot==x.id.slot&&description.uuid==x.id.uuid&&description.targets.size()==3,"Python request descriptor retains selected adapter and three canonical targets");
    auto requests=stage_voltage_tables_profile(document,data);check(requests.size()==3&&requests[0].value==INT32_MIN&&requests[1].value==INT32_MAX&&requests[2].key.field==5&&requests[0].expected==1000&&x.reads==reads&&x.sets==0,"Python six-word staging preserves signed endpoints, original baselines and performs no reads/writes");
    auto subset=select_voltage_tables_profile(document,{"rail:1:1","voltage:request:1:5"});check(subset["voltage_requests"]["rails"].size()==1&&stage_voltage_tables_profile(subset,data).size()==2&&describe_voltage_tables_profile(subset).targets.size()==2,"Python subset preserves only selected fields and their original rail identity");
    const auto native=capture_voltage_tables_profile(data,{{VoltageTableFamily::requests,0,0},{VoltageTableFamily::requests,1,5}});check(describe_voltage_tables_profile(native).targets==std::vector<std::string>({"rail:0:0","voltage:request:1:5"})&&stage_voltage_tables_profile(select_voltage_tables_profile(native,{"rail:0:0"}),data).size()==1,"native descriptors share request/rail overlap naming and subset parser");
    check(rejects([&]{select_voltage_tables_profile(document,{"rail:0:0","rail:0:0"});})&&rejects([&]{select_voltage_tables_profile(document,{"rail:0:3"});}),"unknown and duplicate target selection rejected");
    auto bad=document;bad["voltage_requests"]["identity"]["device_id"]=0x4322;check(rejects([&]{stage_voltage_tables_profile(bad,data);}),"Python exact current device identity required");bad=document;bad["voltage_requests"]["identity"]["architecture"]=8;check(rejects([&]{stage_voltage_tables_profile(bad,data);}),"Python architecture change rejected");bad=document;bad["voltage_requests"]["identity"]["subsystem_id"]=1;check(rejects([&]{stage_voltage_tables_profile(bad,data);}),"Python subsystem change rejected");
    bad=document;bad["device"]["uuid"]="GPU-other";check(rejects([&]{stage_voltage_tables_profile(bad,data);}),"Python UUID mismatch rejected");bad=document;bad["device"]["slot"]="0000:01:00.0";check(rejects([&]{describe_voltage_tables_profile(bad);}),"conflicting saved slots rejected structurally");
    bad=document;bad["voltage_requests"]["rails"]["0"]["identity"]["name"]="MSVDD";check(rejects([&]{stage_voltage_tables_profile(bad,data);}),"rail name validated against current policy discriminator");bad=document;bad["voltage_requests"]["rails"]["0"]["identity"]["control_type"]=1;check(rejects([&]{describe_voltage_tables_profile(bad);}),"unknown control type rejected by common parser");
    bad=document;bad["voltage_requests"]["rails"]["0"]["offsets_uv"]={{"REL",true}};check(rejects([&]{describe_voltage_tables_profile(bad);}),"Boolean cannot stand in for signed request word");bad=document;bad["voltage_requests"]["rails"]["0"]["offsets_uv"]={{"raw",1}};check(rejects([&]{describe_voltage_tables_profile(bad);}),"unknown request words never silently dropped");bad=document;bad["packet"]="opaque";check(rejects([&]{describe_voltage_tables_profile(bad);}),"opaque extra scope rejected");bad=document;bad["incomplete"]={"missing rail"};check(rejects([&]{describe_voltage_tables_profile(bad);}),"incomplete saved snapshot refused");bad=native;bad["fields"].push_back(bad["fields"][0]);check(rejects([&]{describe_voltage_tables_profile(bad);}),"native duplicate fields rejected structurally");
    x.admission=false;s.read(1);check(stage_voltage_tables_profile(document,s.state()).size()==3&&x.sets==0,"Python staging is available with gate Off");
    x.public_present=1;s.read(1);auto one=select_voltage_tables_profile(document,{"rail:0:0"});check(stage_voltage_tables_profile(one,s.state()).size()==1&&!s.state().requests_complete,"partial unrelated rail does not remove a readable selected draft");check(rejects([&]{stage_voltage_tables_profile(document,s.state());}),"stale selected sibling prevents complete staging");
    x.public_present=3;x.fail_reads=4;s.read(1);check(rejects([&]{stage_voltage_tables_profile(one,s.state());})&&x.sets==0,"transient last-good data is visible but not valid staging authority");x.fail_reads=0;s.read(1);check(stage_voltage_tables_profile(one,s.state()).size()==1,"bounded recovery permits fresh import again");
    x.admission=true;s.read(1);check(s.apply(stage_voltage_tables_profile(document,s.state())).ok&&x.value(0,0)==INT32_MIN&&x.value(1,1)==INT32_MAX&&x.value(1,5)==1234,"explicit Python-import Apply uses qualified selected signed requests");check(s.undo().ok&&x.value(0,0)==1000&&x.value(1,1)==2111&&x.value(1,5)==2555,"Python-import group Undo restores only selected prior request words");
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
void catalog_transactions(){Fake x;VoltageTablesService s(api(),x.gpu(),x.id);s.read(1);
    check(s.apply(request(s,1,5,9000)).ok,"catalog fixture has meaningful earlier standalone Undo");
    const auto before_sets=x.sets;const auto plan=s.preview({request(s,0,0,5000),request(s,0,1,1111),request(s,1,4,8000)});
    check(plan.ok&&x.sets==before_sets,"whole catalog preview includes no-op selected fields without writes");
    VoltageTablesReceipt receipt;int journals=0;
    auto applied=s.apply_profile(plan,[&](const auto& r){receipt=r;++journals;check(x.sets==before_sets+journals-1,"receipt published before each possible dispatch");});
    check(applied.ok&&journals==2&&s.verify(plan).ok&&s.verify(receipt).ok,"catalog receipt includes only two issued changes; complete selected plan verifies");
    x.set(0,1,12345);check(!s.verify(plan).ok&&s.verify(receipt).ok,"final plan detects interference with selected initial no-op absent from receipt");
    x.set(1,0,7654);x.public_rows[1][0x3C]=0x33;
    check(s.restore(receipt).ok&&s.verify(receipt,true).ok&&x.value(0,0)==1000&&x.value(1,4)==2444&&x.value(0,1)==12345&&x.value(1,0)==7654&&x.public_rows[1][0x3C]==0x33,"receipt restores only issued selections rebased onto current unrelated values and unknown fields");
    check(s.undo().ok&&x.value(1,5)==2555&&x.value(0,1)==12345,"catalog restoration preserves prior meaningful standalone Undo");
    s.read(1);const auto noop=s.preview({request(s,0,0,1000)});const auto count=x.sets;
    check(noop.ok&&s.apply_profile(noop,[&](const auto&){check(false,"no-op must not journal");}).ok&&x.sets==count,"all-no-op catalog plan dispatches and journals nothing");
    x.set(0,0,2222);check(!s.apply_profile(noop,{}).ok&&x.sets==count,"selected plan stale baseline rejected before any SET");
    Fake other;VoltageTablesService other_service(api(),other.gpu(),other.id);other_service.read(1);
    check(!other_service.apply_profile(plan,{}).ok&&!other_service.verify(plan).ok&&!other_service.restore(receipt).ok&&other.sets==0,"even matching target cannot reuse another process-local service plan or receipt");
}
void catalog_qualification(){Fake x;x.info[12+0x33]=5;std::snprintf(x.id.uuid,sizeof(x.id.uuid),"GPU-catalog-alternate");VoltageTablesService s(api(),x.gpu(),x.id);s.read(1);
    x.admission=false;check(!s.preview({request(s,0,0,5000)}).ok,"ordinary preview refuses disabled admission");
    const auto projected=s.preview({request(s,0,0,5000)},true);check(projected.ok&&!x.admission&&x.sets==0,"projected admission permits only read-only whole preflight");
    check(!s.apply_profile(projected,{}).ok&&x.sets==0,"actual Apply rechecks actual gate rather than trusting projected enable");
    x.admission=true;VoltageTablesReceipt r;check(s.apply_profile(projected,[&](const auto& v){r=v;}).ok&&s.restore(r).ok,"same qualified projected plan applies after explicit external gate enable");
    x.exports=false;check(!s.preview({request(s,0,0,5000)}).ok,"whole preview checks actual setter export");x.exports=true;
    x.public_present=1;check(!s.preview({request(s,0,0,5000)}).ok,"whole preview requires complete inventory despite readable selected rail");x.public_present=3;
    x.fail_reads=1;check(s.preview({request(s,0,0,5000)}).ok&&x.refreshes>0,"preview uses bounded read-only transient recovery");
    x.fail_reads=100;const auto sets=x.sets;check(!s.preview({request(s,0,0,5000)}).ok&&x.sets==sets,"exhausted transient preview never writes");x.fail_reads=0;
    const auto identity_plan=s.preview({request(s,0,0,5000)});std::snprintf(x.id.uuid,sizeof(x.id.uuid),"GPU-replaced");
    check(identity_plan.ok&&!s.apply_profile(identity_plan,{}).ok&&x.sets==sets,"current paired identity revalidated before catalog launch");
}
void catalog_partial(){Fake x;VoltageTablesService s(api(),x.gpu(),x.id);s.read(1);check(s.apply(request(s,1,5,9000)).ok,"uncertain catalog fixture earlier edit");
    auto a=api(); // Replace only the injected setter; preserve real packet semantics.
    a.public_set=[](dc_gpu* gpu,uint8_t* bytes,size_t size,int32_t& status,dc_error& error){auto& fake=f(gpu);const auto out=setter(gpu,bytes,size,status,error);if(fake.sets==4){fake.fail_reads=100;status=-1;}return out;};
    VoltageTablesService grouped(a,x.gpu(),x.id);grouped.read(1);check(grouped.apply(request(grouped,1,5,9100)).ok,"grouped service captures its own earlier Undo");
    const auto plan=grouped.preview({request(grouped,0,0,5000),request(grouped,0,1,6000),request(grouped,0,2,7000)});VoltageTablesReceipt receipt;
    // Existing writes count two; the second catalog SET is accepted but unreadable.
    const auto result=grouped.apply_profile(plan,[&](const auto& r){receipt=r;});
    check(!result.ok&&result.may_have_changed&&grouped.state().recovery_pending&&x.sets==4&&x.value(0,0)==5000&&x.value(0,1)==6000&&x.value(0,2)==1222,"accepted-but-failed catalog SET retains only launched subset without blind rollback or third write");
    check(!grouped.preview({request(grouped,0,1,6000)}).ok,"pending external recovery blocks a fresh plan");x.fail_reads=0;
    check(grouped.restore(receipt).ok&&x.value(0,0)==1000&&x.value(0,1)==1111&&grouped.undo().ok&&x.value(1,5)==9000,"explicit receipt recovery restores issued subset and earlier meaningful internal Undo");
    Fake failed;VoltageTablesService fs(api(),failed.gpu(),failed.id);fs.read(1);auto fp=fs.preview({request(fs,0,0,5000)});failed.foreign_after_set=true;VoltageTablesReceipt fr;
    check(!fs.apply_profile(fp,[&](const auto& r){fr=r;}).ok,"foreign replacement fixture retains catalog receipt");const auto count=failed.sets;
    check(!fs.restore(fr).ok&&failed.value(0,0)==987654&&failed.sets==count&&fs.state().recovery_pending,"external selected replacement blocks recovery without erasing receipt or overwriting foreign value");
}
void catalog_chain_and_exceptions(){Fake x;VoltageTablesService s(api(),x.gpu(),x.id);s.read(1);
    check(s.apply(request(s,1,5,9000)).ok,"receipt chain starts with an ordinary edit");
    VoltageTablesReceipt first,second;auto plan=s.preview({request(s,0,0,5000)});
    check(s.apply_profile(plan,[&](const auto& r){first=r;}).ok,"first independent catalog component applies");
    plan=s.preview({request(s,0,1,6000)});check(s.apply_profile(plan,[&](const auto& r){second=r;}).ok,"second independent catalog component applies");
    x.admission=false;check(!s.apply(request(s,0,2,7000)).ok,"unissued standalone edit after catalog refuses disabled admission");x.admission=true;
    check(s.restore(second).ok&&s.restore(first).ok&&s.undo().ok&&x.value(1,5)==2555&&x.value(0,0)==1000&&x.value(0,1)==1111,"reverse receipts preserve the entire earlier internal Undo chain");
    plan=s.preview({request(s,0,0,5000)});const auto count=x.sets;VoltageTablesReceipt cancelled;
    check(!s.apply_profile(plan,[&](const auto& r){cancelled=r;throw std::runtime_error("journal failure");}).ok&&x.sets==count&&!s.state().recovery_pending,"journal exception before launch does not dispatch or manufacture pending recovery");
    check(!s.restore(cancelled).ok,"non-issued journal cannot authorize restoration");
    Fake thrown;auto a=api();a.public_set=[](dc_gpu* gpu,uint8_t* bytes,size_t size,int32_t& status,dc_error& error)->dc_status{
        setter(gpu,bytes,size,status,error);throw std::runtime_error("transport raised after dispatch");};
    VoltageTablesService ts(a,thrown.gpu(),thrown.id);ts.read(1);const auto tp=ts.preview({request(ts,0,0,5000)});VoltageTablesReceipt tr;
    check(!ts.apply_profile(tp,[&](const auto& r){tr=r;}).ok&&thrown.sets==1&&ts.state().recovery_pending,"exception after SET retains uncertain launched receipt");
    // This fake throws after every SET; a failed restore may nevertheless land.
    check(!ts.restore(tr).ok&&thrown.value(0,0)==1000&&ts.state().recovery_pending,"accepted-but-throwing restoration keeps recovery until fresh verification");
    check(ts.restore(tr).ok&&thrown.sets==2&&!ts.state().recovery_pending,"later fresh original readback resolves uncertainty without duplicate SET");
}
void catalog_publication(){Fake x;auto a=api();a.transport.escape=[](dc_gpu* gpu,uint8_t* bytes,size_t size,const dc_escape_fields* fields_arg,int32_t* nt,dc_error* error)->dc_status{
        if(reinterpret_cast<uint32_t*>(bytes)[14]==0x2080B201){
            if(f(gpu).fail_reads==-999){++f(gpu).reads;error->kind=DC_ERR_INVALID;return DC_E_FAILED;}
            if(f(gpu).fail_reads==-888)throw std::runtime_error("fresh sampling exception");
        }return escape(gpu,bytes,size,fields_arg,nt,error);};
    VoltageTablesService s(a,x.gpu(),x.id);s.read(1);auto plan=s.preview({request(s,0,0,5000)});
    check(plan.ok&&!s.verify(plan).ok&&!s.state().rows[0].stale&&s.state().rows[0].availability==VoltagePolicyAvailability::available,"desired mismatch is current valid data, not transient unavailability");
    x.set(0,0,1001);check(!s.apply_profile(plan,{}).ok&&s.state().rows[0].value==1001&&!s.state().rows[0].stale,"stale selected plan rejects SET while publishing freshly sampled replacement");
    check(!s.verify(VoltageTablesPlan{}).ok&&!s.state().rows[0].stale,"foreign or malformed plan does not downgrade established row availability");
    plan=s.preview({request(s,0,0,5000)});x.fail_reads=100;
    check(!s.verify(plan).ok&&s.state().rows[0].stale&&s.state().rows[0].value==1001&&s.state().rows[0].availability==VoltagePolicyAvailability::transient,"exhausted verify capture publishes retained last-good value stale/transient");
    x.fail_reads=0;s.read(1);x.fail_reads=-888;
    check(!s.preview({request(s,0,0,5000)}).ok&&s.state().rows[0].stale&&s.state().rows[0].availability==VoltagePolicyAvailability::transient,"transport exception during preview cannot leave last-good data advertised fresh");
    x.fail_reads=0;s.read(1);const auto reads=x.reads;x.fail_reads=-999;
    check(!s.preview({request(s,0,0,5000)}).ok&&x.reads==reads+1&&s.state().rows[0].availability==VoltagePolicyAvailability::invalid,"malformed/invalid capability status is not retried or mislabeled transient");
}
void catalog_restore_whole_preflight(){Fake x;VoltageTablesService s(api(),x.gpu(),x.id);s.read(1);
    const auto plan=s.preview({request(s,0,0,5000),request(s,1,1,6000)});VoltageTablesReceipt receipt;
    check(s.apply_profile(plan,[&](const auto& r){receipt=r;}).ok,"whole-restore preflight fixture issues both selected fields");
    x.set(0,0,7777);const auto sets=x.sets;
    check(!s.restore(receipt).ok&&x.sets==sets&&x.value(0,0)==7777&&x.value(1,1)==6000&&s.state().recovery_pending,
        "conflict in earlier issued field prevents every restoration SET, including the reverse-first peer");
    check(!s.recover().ok&&x.sets==sets&&x.value(1,1)==6000,"repeated recovery retains whole conflicting receipt without partial mutation");
    x.set(0,0,5000);check(s.restore(receipt).ok&&s.verify(receipt,true).ok&&x.value(0,0)==1000&&x.value(1,1)==2111,
        "after conflict resolves, retained receipt restores both selected originals with fresh final verification");
}
void history_boundary(){Fake x;VoltageTablesService s(api(),x.gpu(),x.id);s.read(1);int calls=0;bool fail_history=false;s.set_history_callback([&]{++calls;if(fail_history)throw std::runtime_error("history refused");});
 auto same=request(s,0,0,1000);check(s.apply(same).ok&&calls==0&&x.sets==0,"No-op table request skips history and SET");x.admission=false;auto desired=request(s,0,0,1100);check(!s.apply(desired).ok&&calls==0&&x.sets==0,"Closed admission skips history");x.admission=true;check(s.apply(desired).ok&&calls==1&&x.sets==1,"Changed request captures once before SET");auto second=request(s,0,0,1200);fail_history=true;check(!s.apply(second).ok&&calls==2&&x.sets==1&&s.state().undo_available,"History failure blocks SET and preserves Undo");check(s.undo().ok&&x.value(0,0)==1000&&calls==2,"Undo bypasses history callback");}
}
int main(){history_boundary();normal();qualification();recovery();profiles();exchange_profiles();nvapi_only();catalog_transactions();catalog_qualification();catalog_partial();catalog_chain_and_exceptions();catalog_publication();catalog_restore_whole_preflight();std::printf("Voltage tables service: %d failures\n",failures);return failures?1:0;}
