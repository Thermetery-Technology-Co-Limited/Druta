// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_saving_fake.h"
#include <cstdlib>
#include <stdexcept>
using namespace power_saving_test;
static int checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
template<class F>static void profile_rejects(F f){bool rejected=false;try{f();}catch(const std::exception&){rejected=true;}CHECK(rejected);}
static ProfileJson python_lowpower(const Fake& f,const char* kind,const char* mode,ProfileJson index=nullptr){return {{"schema",2},{"scope","lowpower_requests"},{"saved_at","2026-11-13"},{"saved_ts",1},{"device",{{"name","alternate"},{"vbios",nullptr},{"driver",nullptr},{"uuid",f.identity.uuid},{"slot",f.identity.slot}}},{"incomplete",ProfileJson::array()},{"lowpower_requests",{{"kind",kind},{"index",index},{"mode",mode},{"identity",{{"slot",f.identity.slot},{"architecture",7},{"device_id",f.identity.nvapi_devid},{"subsystem_id",f.identity.nvapi_subsys}}}}}};}
static void catalog_transactions(){
    {Fake f;PowerSavingService s(api(),f.gpu(),f.identity);s.read();CHECK(s.apply(request(s,PowerSavingFamily::idle_threshold,12,8000)).ok);
        const auto plan=s.preview({request(s,PowerSavingFamily::individual,0,1),request(s,PowerSavingFamily::idle_threshold,0,6000),request(s,PowerSavingFamily::idle_threshold,4,2000)});CHECK(plan.ok&&f.sets==1);
        PowerSavingReceipt receipt;int journals=0;CHECK(s.apply_profile(plan,[&](const auto& r){receipt=r;++journals;CHECK(f.sets==journals);}).ok);CHECK(journals==2&&s.verify(plan).ok&&s.verify(receipt).ok);
        f.raw[4]=3333;CHECK(!s.verify(plan).ok&&s.verify(receipt).ok);CHECK(s.restore(receipt).ok&&s.verify(receipt,true).ok&&f.raw[0]==2000&&f.raw[4]==3333&&f.reasons[0]==0&&f.other[0]==0x80);
        CHECK(s.undo().ok&&f.raw[12]==2000);CHECK(f.lock_depth==0);
        const auto noop=s.preview({request(s,PowerSavingFamily::idle_threshold,0,2000)});const auto sets=f.sets;CHECK(noop.ok&&s.apply_profile(noop,[&](const auto&){CHECK(false);}).ok&&f.sets==sets);
        f.raw[0]=4000;CHECK(!s.apply_profile(noop,{}).ok&&f.sets==sets&&row(s,PowerSavingFamily::idle_threshold).value==4000&&!row(s,PowerSavingFamily::idle_threshold).stale);
    }
    {Fake f;f.supported[12]=false;PowerSavingService s(api(),f.gpu(),f.identity);s.read();CHECK(!s.preview({request(s,PowerSavingFamily::individual,0,1),request(s,PowerSavingFamily::individual,12,1)}).ok&&f.sets==0);
        CHECK(s.preview({request(s,PowerSavingFamily::individual,4,1)}).ok);CHECK(!s.preview({request(s,PowerSavingFamily::adaptive,0,0)}).ok);
        CHECK(s.apply(request(s,PowerSavingFamily::adaptive,0,0)).ok);const auto sets=f.sets;
        CHECK(!s.preview({request(s,PowerSavingFamily::adaptive,0,1),request(s,PowerSavingFamily::idle_threshold,0,6000)}).ok&&f.sets==sets);
        CHECK(!s.preview({request(s,PowerSavingFamily::gr_elpg,0,1),request(s,PowerSavingFamily::individual,0,1)}).ok&&f.allocations==0&&f.sets==sets);
        CHECK(!s.preview({request(s,PowerSavingFamily::gr_elpg,0,1),request(s,PowerSavingFamily::difr,0,1)}).ok&&f.allocations==0);
        auto plan=s.preview({request(s,PowerSavingFamily::adaptive,0,1)});PowerSavingReceipt receipt;CHECK(plan.ok&&s.apply_profile(plan,[&](const auto& r){receipt=r;}).ok&&s.verify(plan).ok&&s.restore(receipt).ok&&!f.adaptive[0]);
    }
    {Fake f;PowerSavingService s(api(),f.gpu(),f.identity);s.read(2);const auto plan=s.preview({request(s,PowerSavingFamily::individual,4,1)});CHECK(plan.ok);
        Fake other;PowerSavingService foreign(api(),other.gpu(),other.identity);CHECK(!foreign.apply_profile(plan,{}).ok&&other.sets==0);
        f.fail_gets=1;CHECK(s.preview({request(s,PowerSavingFamily::individual,4,1)}).ok&&f.reset_count>0);f.fail_gets=1000;CHECK(!s.preview({request(s,PowerSavingFamily::individual,4,1)}).ok&&row(s,PowerSavingFamily::individual,4).stale);
        f.fail_gets=0;s.read(2);f.foreign_read=true;CHECK(!s.apply_profile(plan,{}).ok&&f.sets==0&&row(s,PowerSavingFamily::individual,4).availability==VoltagePolicyAvailability::invalid);
    }
    {Fake f;auto a=api();a.transport.escape=[](dc_gpu* gpu,uint8_t* bytes,size_t size,const dc_escape_fields* fields,int32_t* nt,dc_error* error)->dc_status{
            if(reinterpret_cast<uint32_t*>(bytes)[14]==0x2080E802&&power_saving_test::f(gpu).sets==1)power_saving_test::f(gpu).fail_set=true;return escape(gpu,bytes,size,fields,nt,error);};
        PowerSavingService s(a,f.gpu(),f.identity);s.read();const auto plan=s.preview({request(s,PowerSavingFamily::individual,0,1),request(s,PowerSavingFamily::idle_threshold,0,6000),request(s,PowerSavingFamily::individual,4,1)});PowerSavingReceipt receipt;
        const auto result=s.apply_profile(plan,[&](const auto& r){receipt=r;});CHECK(!result.ok&&result.may_have_changed&&f.sets==2&&f.reasons[0]==1&&f.raw[0]==6000&&f.reasons[4]==0&&s.state().recovery_pending);
        CHECK(!s.preview({request(s,PowerSavingFamily::individual,4,1)}).ok);f.fail_set=false;CHECK(s.restore(receipt).ok&&s.verify(receipt,true).ok&&f.raw[0]==2000&&f.reasons[0]==0);
    }
    {Fake f;PowerSavingService s(api(),f.gpu(),f.identity);s.read();auto plan=s.preview({request(s,PowerSavingFamily::difr,0,1)});PowerSavingReceipt receipt;f.fail_set=true;
        CHECK(!s.apply_profile(plan,[&](const auto& r){receipt=r;}).ok);const auto sets=f.sets;f.fail_set=false;CHECK(!s.restore(receipt).ok&&f.sets==sets&&s.state().recovery_pending);CHECK(!s.release_owned().ok&&f.sets==sets);
    }
    {Fake f;auto a=api();a.transport.escape=[](dc_gpu* gpu,uint8_t* bytes,size_t size,const dc_escape_fields* fields,int32_t* nt,dc_error* error)->dc_status{
            if(reinterpret_cast<uint32_t*>(bytes)[14]==0xB0CC0301){error->kind=DC_ERR_UNSUPPORTED;return DC_E_UNAVAILABLE;}return escape(gpu,bytes,size,fields,nt,error);};
        PowerSavingService s(a,f.gpu(),f.identity);s.read(1);const auto plan=s.preview({request(s,PowerSavingFamily::gr_elpg,0,1)});PowerSavingReceipt receipt;bool allocation_journal=false;
        const auto result=s.apply_profile(plan,[&](const auto& r){receipt=r;if(f.allocations==0)allocation_journal=true;});CHECK(!result.ok&&result.may_have_changed&&allocation_journal&&f.allocations==1&&f.sets==0&&s.has_owned_requests());
        CHECK(!f.profiler_request);const auto restored=s.restore(receipt);
        if(!restored.ok)std::fprintf(stderr,"Confirmed orphan restore: %s\n",restored.message.c_str());
        CHECK(restored.ok&&f.handle==0&&f.frees==1&&f.sets==0&&!s.has_owned_requests()&&s.verify(receipt,true).ok);
    }
    {Fake f;auto a=api();a.transport.escape=[](dc_gpu* gpu,uint8_t* bytes,size_t size,const dc_escape_fields* fields,int32_t* nt,dc_error* error)->dc_status{
            const bool allocate=reinterpret_cast<uint32_t*>(bytes)[4]==0x0500002A;const auto status=escape(gpu,bytes,size,fields,nt,error);if(allocate)*nt=-1;return status;};
        PowerSavingService s(a,f.gpu(),f.identity);s.read(1);const auto plan=s.preview({request(s,PowerSavingFamily::gr_elpg,0,1)});PowerSavingReceipt receipt;CHECK(!s.apply_profile(plan,[&](const auto& r){receipt=r;}).ok&&f.allocations==1&&s.state().recovery_pending);
        CHECK(!s.restore(receipt).ok&&!s.release_owned().ok&&f.frees==0); // Unknown allocation cannot authorize FREE.
    }
    {Fake f;auto a=api();a.transport.escape=[](dc_gpu* gpu,uint8_t* bytes,size_t size,const dc_escape_fields* fields,int32_t* nt,dc_error* error)->dc_status{
            const bool free=reinterpret_cast<uint32_t*>(bytes)[4]==0x0500000C;const auto status=escape(gpu,bytes,size,fields,nt,error);if(free)*nt=-1;return status;};
        PowerSavingService s(a,f.gpu(),f.identity);s.read(1);const auto plan=s.preview({request(s,PowerSavingFamily::gr_elpg,0,1)});PowerSavingReceipt receipt;CHECK(s.apply_profile(plan,[&](const auto& r){receipt=r;}).ok);
        CHECK(!s.restore(receipt).ok&&f.frees==1&&s.state().recovery_pending);const auto sets=f.sets;CHECK(!s.restore(receipt).ok&&f.frees==1&&f.sets==sets); // An uncertain FREE is never repeated.
    }
    for(int variant=0;variant<3;++variant){Fake f;static int malformed_allocation=0;malformed_allocation=variant;auto a=api();a.transport.escape=[](dc_gpu* gpu,uint8_t* bytes,size_t size,const dc_escape_fields* fields,int32_t* nt,dc_error* error)->dc_status{
            auto* packet=reinterpret_cast<uint32_t*>(bytes);const bool allocate=packet[4]==0x0500002A;const auto status=escape(gpu,bytes,size,fields,nt,error);
            if(allocate){if(malformed_allocation==0)packet[6]^=1;else if(malformed_allocation==1)packet[-1]^=1;else power_saving_test::f(gpu).foreign_read=true;}return status;};
        PowerSavingService s(a,f.gpu(),f.identity);s.read(1);const auto plan=s.preview({request(s,PowerSavingFamily::gr_elpg,0,1)});PowerSavingReceipt receipt;
        CHECK(!s.apply_profile(plan,[&](const auto& r){receipt=r;}).ok&&f.allocations==1&&f.handle&&f.sets==0&&s.state().recovery_pending&&s.has_owned_requests());
        CHECK(!s.restore(receipt).ok&&!s.release_owned().ok&&f.frees==0&&f.sets==0); // Guard/envelope/target failure never authorizes candidate adoption or FREE.
    }
    {Fake f;auto a=api();a.transport.escape=[](dc_gpu* gpu,uint8_t* bytes,size_t size,const dc_escape_fields* fields,int32_t* nt,dc_error* error)->dc_status{
            auto* packet=reinterpret_cast<uint32_t*>(bytes);const bool free=packet[4]==0x0500000C;const auto status=escape(gpu,bytes,size,fields,nt,error);if(free)packet[6]^=1;return status;};
        PowerSavingService s(a,f.gpu(),f.identity);s.read(1);const auto plan=s.preview({request(s,PowerSavingFamily::gr_elpg,0,1)});PowerSavingReceipt receipt;CHECK(s.apply_profile(plan,[&](const auto& r){receipt=r;}).ok);
        CHECK(!s.restore(receipt).ok&&f.handle==0&&f.frees==1&&s.state().recovery_pending&&s.has_owned_requests());const auto sets=f.sets;
        CHECK(!s.restore(receipt).ok&&!s.release_owned().ok&&f.frees==1&&f.sets==sets); // Malformed accepted deletion retains unresolved lifecycle, never retries a numeric handle.
    }
    for(const auto family:{PowerSavingFamily::individual,PowerSavingFamily::gr_elpg}){
        Fake f;PowerSavingService s(api(),f.gpu(),f.identity);s.read();const auto plan=s.preview({request(s,family,0,1)});PowerSavingReceipt receipt;CHECK(s.apply_profile(plan,[&](const auto& r){receipt=r;}).ok);
        CHECK(s.apply(request(s,family,0,0)).ok&&s.apply(request(s,family,0,1)).ok);const auto sets=f.sets,frees=f.frees;
        CHECK(!s.restore(receipt).ok&&!s.verify(plan).ok&&f.sets==sets&&f.frees==frees);
        const auto replacement=s.preview({request(s,family,0,0)});PowerSavingReceipt replacement_receipt;CHECK(s.apply_profile(replacement,[&](const auto& r){replacement_receipt=r;}).ok&&s.restore(replacement_receipt).ok);
        const auto after_sets=f.sets,after_frees=f.frees;CHECK(!s.restore(receipt).ok&&f.sets==after_sets&&f.frees==after_frees);CHECK(s.release_owned().ok);
    }
    for(const auto family:{PowerSavingFamily::individual,PowerSavingFamily::gr_elpg}){
        Fake f;PowerSavingService s(api(),f.gpu(),f.identity);s.read();CHECK(s.apply(request(s,PowerSavingFamily::idle_threshold,12,8000)).ok);PowerSavingReceipt first,second;
        auto plan=s.preview({request(s,family,0,1)});CHECK(s.apply_profile(plan,[&](const auto& r){first=r;}).ok);plan=s.preview({request(s,family,0,0)});CHECK(s.apply_profile(plan,[&](const auto& r){second=r;}).ok);
        CHECK(s.restore(second).ok&&s.verify(first).ok&&s.restore(first).ok&&s.undo().ok&&f.raw[12]==2000&&!s.has_owned_requests());
    }
    {Fake f;PowerSavingService s(api(),f.gpu(),f.identity);s.read(2);const auto plan=s.preview({request(s,PowerSavingFamily::individual,0,1)});PowerSavingReceipt receipt;
        CHECK(!s.apply_profile(plan,[&](const auto& r){receipt=r;throw std::runtime_error("journal failed");}).ok&&f.sets==0&&!s.state().recovery_pending);CHECK(!s.restore(receipt).ok);
    }
    {Fake f;auto a=api();a.transport.escape=[](dc_gpu* gpu,uint8_t* bytes,size_t size,const dc_escape_fields* fields,int32_t* nt,dc_error* error)->dc_status{
            const bool set=reinterpret_cast<uint32_t*>(bytes)[14]==0x2080E802;const auto status=escape(gpu,bytes,size,fields,nt,error);if(set&&power_saving_test::f(gpu).sets==1)throw std::runtime_error("accepted then raised");return status;};
        PowerSavingService s(a,f.gpu(),f.identity);s.read(2);const auto plan=s.preview({request(s,PowerSavingFamily::individual,0,1)});PowerSavingReceipt receipt;CHECK(!s.apply_profile(plan,[&](const auto& r){receipt=r;}).ok&&s.state().recovery_pending&&f.sets==1);CHECK(s.restore(receipt).ok&&f.reasons[0]==0&&f.sets==2);
    }
    {Fake f;auto a=api();a.transport.escape=[](dc_gpu* gpu,uint8_t* bytes,size_t size,const dc_escape_fields* fields,int32_t* nt,dc_error* error)->dc_status{
            const bool set=reinterpret_cast<uint32_t*>(bytes)[14]==0x2080E802;const auto status=escape(gpu,bytes,size,fields,nt,error);if(set&&power_saving_test::f(gpu).sets==3)power_saving_test::f(gpu).raw[0]=7777;return status;};
        PowerSavingService s(a,f.gpu(),f.identity);s.read(4);const auto plan=s.preview({request(s,PowerSavingFamily::idle_threshold,0,6000),request(s,PowerSavingFamily::idle_threshold,4,7000)});PowerSavingReceipt receipt;CHECK(s.apply_profile(plan,[&](const auto& r){receipt=r;}).ok);
        CHECK(!s.restore(receipt).ok&&f.sets==3&&f.raw[0]==7777&&f.raw[4]==2000);CHECK(!s.restore(receipt).ok&&f.sets==3); // Per-step conflict never authorizes a new selected value.
    }
    {Fake f;PowerSavingService s(api(),f.gpu(),f.identity);s.read();auto plan=s.preview({request(s,PowerSavingFamily::individual,0,1),request(s,PowerSavingFamily::idle_threshold,0,6000)});PowerSavingReceipt receipt;CHECK(s.apply_profile(plan,[&](const auto& r){receipt=r;}).ok);CHECK(s.release_owned().ok&&f.raw[0]==6000&&f.reasons[0]==0);}
    {Fake f;static bool fail_restore_read=false;static int support_reads=0;auto a=api();a.transport.escape=[](dc_gpu* gpu,uint8_t* bytes,size_t size,const dc_escape_fields* fields,int32_t* nt,dc_error* error)->dc_status{
            if(fail_restore_read&&reinterpret_cast<uint32_t*>(bytes)[14]==0x2080A801&&half(bytes+68,4)==4&&half(bytes+68,6)==0&&half(bytes+68,8)==9&&++support_reads>=3){*nt=-1;return DC_OK;}return escape(gpu,bytes,size,fields,nt,error);};
        PowerSavingService s(a,f.gpu(),f.identity);s.read(8);CHECK(s.apply(request(s,PowerSavingFamily::adaptive,0,0)).ok);const auto plan=s.preview({request(s,PowerSavingFamily::adaptive,0,1)});PowerSavingReceipt receipt;f.fail_set=true;
        CHECK(!s.apply_profile(plan,[&](const auto& r){receipt=r;}).ok&&s.state().recovery_pending&&f.adaptive[0]);f.fail_set=false;fail_restore_read=true;support_reads=0;
        CHECK(!s.restore(receipt).ok&&s.state().recovery_pending&&f.sets==2&&f.adaptive[0]);fail_restore_read=false;
        CHECK(s.restore(receipt).ok&&f.sets==3&&!f.adaptive[0]&&s.verify(receipt,true).ok); // Cached prior intent cannot prove an uncertain SET undone.
    }
}
int main(){
    catalog_transactions();
    {Fake f;f.supported[12]=false;PowerSavingService s(api(),f.gpu(),f.identity);s.read();auto python=python_lowpower(f,"individual","disabled",4);const auto description=describe_power_saving_profile(python);CHECK(description.targets==std::vector<std::string>{"saving:1:4"});auto requests=stage_power_saving_profile(python,s.state());CHECK(requests.size()==1&&requests[0].key.index==4&&requests[0].value==1&&requests[0].expected==0&&f.sets==0&&f.allocations==0);CHECK(select_power_saving_profile(python,{"saving:1:4"})==python);
        auto wrong=python;wrong["lowpower_requests"]["identity"]["architecture"]=8;profile_rejects([&]{stage_power_saving_profile(wrong,s.state());});wrong=python;wrong["lowpower_requests"]["index"]=3;profile_rejects([&]{describe_power_saving_profile(wrong);});wrong=python;wrong["lowpower_requests"]["mode"]="enabled";profile_rejects([&]{describe_power_saving_profile(wrong);});wrong=python;wrong["incomplete"].push_back("failed");profile_rejects([&]{split_saving_profile(wrong);});
        auto native=capture_power_saving_profile(s.state(),{{PowerSavingFamily::gr_elpg,0},{PowerSavingFamily::individual,4}});CHECK(describe_power_saving_profile(native).targets.size()==2);auto subset=select_power_saving_profile(native,{"saving:1:4"});CHECK(subset["requests"].size()==1&&stage_power_saving_profile(subset,s.state()).size()==1);native["requests"][0]["value"]=-1;profile_rejects([&]{select_power_saving_profile(native,{"saving:1:4"});});
        auto clock=python_lowpower(f,"elcg","disabled");ProfileJson combined={{"schema",2},{"scope","combined"},{"saved_at","today"},{"saved_ts",2},{"components",ProfileJson::array({{{"name","saving"},{"state",python}},{{"name","clock"},{"state",clock}}})}};auto split=split_saving_profile(combined);CHECK(split.power_saving.size()==1&&split.clock_gating.size()==1);profile_rejects([&]{stage_power_saving_profile(combined,s.state());});combined["components"][1]["state"]["lowpower_requests"]["mode"]="invalid";profile_rejects([&]{split_saving_profile(combined);});combined["components"][1]["state"]=python;profile_rejects([&]{split_saving_profile(combined);});
        auto released=python;released["lowpower_requests"]["mode"]="released";auto aggregate=s.state();for(auto& r:aggregate.rows)if(r.key==PowerSavingKey{PowerSavingFamily::individual,4})r.aggregate_enabled=false;const auto release_requests=stage_power_saving_profile(released,aggregate);CHECK(release_requests[0].value==0&&release_requests[0].expected==0);
        auto stale=s.state();for(auto& r:stale.rows)if(r.key==PowerSavingKey{PowerSavingFamily::individual,4})r.stale=true;profile_rejects([&]{stage_power_saving_profile(python,stale);});CHECK(f.sets==0&&f.allocations==0);
    }
    {Fake f;PowerSavingService s(api(),f.gpu(),f.identity);s.read();CHECK(f.sets==0&&f.allocations==0);CHECK(row(s,PowerSavingFamily::idle_threshold).writable);CHECK(row(s,PowerSavingFamily::idle_threshold).scale==1000);CHECK(!row(s,PowerSavingFamily::adaptive).value&&!row(s,PowerSavingFamily::adaptive).initial);CHECK(row(s,PowerSavingFamily::adaptive,4).label=="Adaptive power: NVDEC"&&row(s,PowerSavingFamily::individual,4).label=="Individual power gating: Memory subsystem (MS)");
        CHECK(s.apply(request(s,PowerSavingFamily::individual,0,1)).ok);CHECK(f.reasons[0]==1&&f.other[0]==0x80&&s.has_owned_requests());CHECK(s.undo().ok&&f.reasons[0]==0&&!s.has_owned_requests());CHECK(f.lock_depth==0);
        CHECK(s.apply(request(s,PowerSavingFamily::idle_threshold,0,4000)).ok);CHECK(f.raw[0]==4000&&f.second[0]==3000);CHECK(s.undo().ok&&f.raw[0]==2000);CHECK(s.apply(request(s,PowerSavingFamily::idle_threshold,0,5000)).ok);CHECK(s.restore_initial({PowerSavingFamily::idle_threshold,0}).ok&&f.raw[0]==2000);}
    {Fake f;f.native_arch=0x170;f.implementation=4;f.supported[12]=false;PowerSavingService s(api(),f.gpu(),f.identity);s.read();CHECK(row(s,PowerSavingFamily::individual,4).writable);CHECK(!row(s,PowerSavingFamily::individual,12).writable);CHECK(!row(s,PowerSavingFamily::idle_threshold).writable&&!row(s,PowerSavingFamily::idle_threshold).stale&&row(s,PowerSavingFamily::idle_threshold).availability==VoltagePolicyAvailability::unsupported);CHECK(!row(s,PowerSavingFamily::adaptive).stale&&row(s,PowerSavingFamily::adaptive).availability==VoltagePolicyAvailability::unsupported);CHECK(row(s,PowerSavingFamily::individual,4).label=="Individual power gating: Memory subsystem (MS)");CHECK(row(s,PowerSavingFamily::difr).writable);CHECK(s.apply(request(s,PowerSavingFamily::individual,4,1)).ok);CHECK(s.release_owned().ok);}
    {Fake f;PowerSavingService s(api(),f.gpu(),f.identity);f.fail_gets=1;s.read(2);CHECK(row(s,PowerSavingFamily::individual).writable&&f.reset_count==1);f.fail_gets=1000;s.read(2);CHECK(row(s,PowerSavingFamily::individual).stale&&!row(s,PowerSavingFamily::individual).writable);f.fail_gets=0;s.read(2);CHECK(!row(s,PowerSavingFamily::individual).stale);const int resets=f.reset_count;f.malformed=true;s.read(2);CHECK(f.reset_count==resets);}
    {Fake f;PowerSavingService s(api(),f.gpu(),f.identity);s.read(2);f.fail_set=true;CHECK(!s.apply(request(s,PowerSavingFamily::individual,0,1)).ok);CHECK(s.state().recovery_pending&&row(s,PowerSavingFamily::individual).owned);const int writes=f.sets;CHECK(!s.apply(request(s,PowerSavingFamily::individual,0,1)).ok&&f.sets==writes);f.fail_set=false;CHECK(s.recover().ok&&f.reasons[0]==0);}
    {Fake f;PowerSavingService s(api(),f.gpu(),f.identity);s.read(2);CHECK(s.apply(request(s,PowerSavingFamily::individual,0,1)).ok);f.fail_set=true;CHECK(!s.apply(request(s,PowerSavingFamily::individual,0,0)).ok);const int writes=f.sets;f.fail_set=false;CHECK(s.recover().ok&&f.sets==writes);}
    {Fake f;PowerSavingService s(api(),f.gpu(),f.identity);s.read(16);CHECK(s.apply(request(s,PowerSavingFamily::difr,0,1)).ok);CHECK(f.other[9]==0x81);CHECK(s.apply(request(s,PowerSavingFamily::difr,0,0)).ok&&f.other[9]==0x80);f.fail_set=true;CHECK(!s.apply(request(s,PowerSavingFamily::difr,0,1)).ok);const int writes=f.sets;CHECK(!s.recover().ok&&f.sets==writes);CHECK(!s.release_owned().ok&&f.sets==writes);}
    {Fake f;PowerSavingService s(api(),f.gpu(),f.identity);s.read(1);CHECK(s.apply(request(s,PowerSavingFamily::gr_elpg,0,1)).ok);CHECK(f.allocations==1&&f.handle&&s.has_owned_requests());CHECK(s.release_owned().ok);CHECK(f.handle==0&&f.frees==1&&!s.has_owned_requests());}
    {Fake f;PowerSavingService s(api(),f.gpu(),f.identity);s.read(8);CHECK(s.apply(request(s,PowerSavingFamily::adaptive,0,0)).ok);CHECK(!row(s,PowerSavingFamily::adaptive).initial&&!s.state().undo_available);CHECK(s.apply(request(s,PowerSavingFamily::adaptive,0,1)).ok&&s.state().undo_available);CHECK(s.undo().ok&&!f.adaptive[0]);}
    {Fake f;PowerSavingService s(api(),f.gpu(),f.identity);s.read(4);f.fail_set=true;CHECK(!s.apply(request(s,PowerSavingFamily::idle_threshold,0,6000)).ok);f.fail_set=false;CHECK(s.recover().ok&&f.raw[0]==2000);CHECK(s.apply(request(s,PowerSavingFamily::idle_threshold,0,7000)).ok);f.raw[0]=9000;const int writes=f.sets;CHECK(!s.undo().ok&&f.sets==writes);}
    {Fake f;PowerSavingService s(api(),f.gpu(),f.identity);s.read(4);f.skip_raw=true;s.read(4);CHECK(row(s,PowerSavingFamily::idle_threshold).stale);}
    {Fake f;f.identity.uuid[0]=0;PowerSavingService s(api(),f.gpu(),f.identity);s.read(2);CHECK(s.apply(request(s,PowerSavingFamily::individual,0,1)).ok);std::strcpy(f.identity.uuid,"GPU-appeared");const int writes=f.sets;CHECK(!s.apply(request(s,PowerSavingFamily::individual,0,0)).ok&&f.sets==writes);}
    {Fake f;PowerSavingService s(api(),f.gpu(),f.identity);s.read(2);f.no_dispatch=true;CHECK(!s.apply(request(s,PowerSavingFamily::individual,0,1)).ok&&!s.state().recovery_pending);CHECK(f.sets==0);}
    {Fake f;PowerSavingService s(api(),f.gpu(),f.identity);s.read();const std::vector<PowerSavingKey> keys{{PowerSavingFamily::individual,0},{PowerSavingFamily::idle_threshold,0}};auto document=capture_power_saving_profile(s.state(),keys);auto requests=stage_power_saving_profile(document,s.state());CHECK(requests.size()==2&&f.sets==0);f.implementation=5;s.read();bool refused=false;try{stage_power_saving_profile(document,s.state());}catch(...){refused=true;}CHECK(refused);}
    {Fake f;PowerSavingService s(api(),f.gpu(),f.identity);s.read(4);f.fail_set=true;CHECK(!s.apply(request(s,PowerSavingFamily::idle_threshold,0,6000)).ok);CHECK(s.state().recovery_pending&&!s.has_owned_requests());const int writes=f.sets;CHECK(s.release_owned().ok&&f.sets==writes&&f.raw[0]==6000);}
    {Fake f;PowerSavingService s(api(),f.gpu(),f.identity);s.read(8);f.fail_set=true;CHECK(!s.apply(request(s,PowerSavingFamily::adaptive,0,0)).ok);CHECK(s.state().recovery_pending&&!s.has_owned_requests());const int writes=f.sets;CHECK(s.release_owned().ok&&f.sets==writes&&!f.adaptive[0]);}
    {Fake f;PowerSavingService s(api(),f.gpu(),f.identity);s.read(8);CHECK(s.apply(request(s,PowerSavingFamily::adaptive,0,0)).ok);f.implementation=5;s.read(8);CHECK(!row(s,PowerSavingFamily::adaptive).value&&!row(s,PowerSavingFamily::adaptive).initial);}
    {Fake f;f.factor=2000;f.raw[0]=3;f.second[0]=3000000;PowerSavingService s(api(),f.gpu(),f.identity);s.read(4);CHECK(row(s,PowerSavingFamily::idle_threshold).writable&&!row(s,PowerSavingFamily::idle_threshold).initial);CHECK(s.apply(request(s,PowerSavingFamily::idle_threshold,0,1000)).ok);CHECK(!row(s,PowerSavingFamily::idle_threshold).initial&&!s.state().undo_available);CHECK(!s.restore_initial({PowerSavingFamily::idle_threshold,0}).ok);}
    {Fake f;PowerSavingService s(api(),f.gpu(),f.identity);s.read(2);CHECK(s.apply(request(s,PowerSavingFamily::individual,0,1)).ok);f.fail_set=true;CHECK(!s.apply(request(s,PowerSavingFamily::individual,4,1)).ok);f.fail_set=false;CHECK(s.release_owned().ok&&!s.has_owned_requests()&&f.reasons[0]==0&&f.reasons[4]==0);}
    std::printf("Power-saving service: %d checks\n",checks);return 0;
}
