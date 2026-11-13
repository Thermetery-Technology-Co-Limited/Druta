// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Actual CoreClient/catalog composition over injected transports. No GPU access.
#include "core_client.h"
#include "profile_catalog.h"
#include "voltage_tables_fake.h"
#include <stdexcept>
using namespace druta::ui;
using namespace voltage_tables_test;
namespace {
int failures=0;
void check(bool ok,const char* message){if(!ok){++failures;std::printf("FAIL: %s\n",message);}}
struct CatalogFake:Fake {
    std::vector<uint8_t> device_info=std::vector<uint8_t>(0x708),device_control=std::vector<uint8_t>(0x188);
    std::vector<std::string> trace;
    int device_sets=0,blocked_device_reads=0;
    bool fail_device_write=false,interfere_on_gate_off=false;
    CatalogFake(){device_info[0]=device_control[0]=1;put(device_info,4,3);put(device_control,4,3);
        for(size_t i=0;i<2;++i){auto at=8+i*56,ct=8+i*12;device_info[at]=device_info[at+1]=device_control[ct]=device_control[ct+1]=3;
            device_info[at+2]=1;device_info[at+3]=uint8_t(i+2);device_info[at+4]=1;put(device_info,at+8,600000);put(device_info,at+12,1300000);put(device_control,ct+4,uint32_t(20+i));put(device_control,ct+8,0xABCDEF);}}
};
CatalogFake* active=nullptr;
CatalogFake& cf(dc_gpu* gpu){return static_cast<CatalogFake&>(f(gpu));}
uint32_t DC_CALL version(){return DC_ABI_VERSION;}
dc_status DC_CALL enumerate(dc_gpu_entry*,size_t,size_t* count){*count=0;return DC_OK;}
dc_status DC_CALL open_gpu(const char* slot,dc_gpu** gpu){if(std::strcmp(slot,active->id.slot))return DC_E_ARGUMENT;*gpu=active->gpu();return DC_OK;}
void DC_CALL close_gpu(dc_gpu*){}
dc_status DC_CALL combined_escape(dc_gpu* gpu,uint8_t* raw,size_t size,const dc_escape_fields* fields,int32_t* nt,dc_error* error){
    auto& x=cf(gpu);auto* p=reinterpret_cast<uint32_t*>(raw);auto* payload=raw+68;const auto command=p[14];
    if(command==0x2080A031){const bool desired=p[18]!=0;const auto prior=x.admission;x.admission=desired;x.trace.push_back(desired?"gate:on":"gate:off");p[17]=prior?1u:0u;p[5]=p[11]=1;p[16]=0;*nt=0;
        if(!desired&&x.interfere_on_gate_off){x.interfere_on_gate_off=false;x.set(0,1,9876);}return DC_OK;}
    if(command==0x2080B205||command==0x2080B207||command==0x2080F208){
        p[5]=p[11]=1;p[16]=0;*nt=0;
        if(command!=0x2080F208&&x.blocked_device_reads>0){--x.blocked_device_reads;*nt=-1;return DC_OK;}
        if(command==0x2080B205)std::memcpy(payload,x.device_info.data(),x.device_info.size());
        else if(command==0x2080B207)std::memcpy(payload,x.device_control.data(),x.device_control.size());
        else{++x.device_sets;x.trace.push_back("delay");for(uint32_t i=0;i<2;++i)if(p[18]&(1u<<i))std::memcpy(x.device_control.data()+12+i*12,payload+12+i*12,4);
            if(x.fail_device_write){x.fail_device_write=false;*nt=-1;x.blocked_device_reads=100;}}
        return DC_OK;
    }
    return escape(gpu,raw,size,fields,nt,error);
}
std::unique_ptr<IBackend> client(CatalogFake& x){active=&x;CoreApi a;a.abi_version=version;a.enumerate=enumerate;a.open=open_gpu;a.close=close_gpu;a.identity=identity;a.lock=lock;a.unlock=unlock;
    a.voltage_tables=api();a.voltage_tables.transport.escape=combined_escape;
    a.voltage_tables.public_set=[](dc_gpu* gpu,uint8_t* bytes,size_t size,int32_t& status,dc_error& error){cf(gpu).trace.push_back("requests");return setter(gpu,bytes,size,status,error);};
    a.voltage_policy=a.voltage_tables.transport;a.voltage_limits.transport=a.voltage_tables.transport;
    auto backend=create_core_backend_for_test(a);std::string reason;check(backend->open(x.id.slot,reason),"open alternate paired catalog adapter");return backend;
}
Command action(const ProfileJson& doc,bool preview=false){Command c;c.operation=preview?Command::Operation::preflight_catalog_profile:Command::Operation::apply_catalog_profile;c.catalog_profile=std::make_shared<const ProfileJson>(doc);return c;}
ActionResult execute(IBackend& backend,const ProfileJson& doc,bool preview=false){auto result=backend.execute(action(doc,preview));if(!result.ok)std::printf("Catalog outcome: %s\n",result.message.c_str());return result;}
Command undo(){Command c;c.operation=Command::Operation::undo;return c;}
ProfileJson requests(CatalogFake& x,std::initializer_list<std::pair<uint32_t,int32_t>> values){ProfileJson fields=ProfileJson::array();for(auto v:values)fields.push_back({{"rail",0},{"field",v.first},{"policy_type",1},{"control_type",0},{"value_uv",v.second}});return {{"format","druta-native-voltage-requests"},{"version",1},{"target",{{"uuid",x.id.uuid},{"slot",x.id.slot}}},{"fields",fields}};}
ProfileJson gate(CatalogFake& x,bool enabled){return {{"format","druta-native-voltage-policy"},{"version",1},{"uuid",x.id.uuid},{"identity",{{"slot",x.id.slot},{"architecture",x.arch},{"device_id",x.id.nvapi_devid},{"subsystem_id",x.id.nvapi_subsys}}},{"selection",1},{"values",enabled?1:0}};}
ProfileJson delays(CatalogFake& x,uint32_t index,uint32_t value){return {{"schema",2},{"scope","voltage_controls"},{"device",{{"uuid",x.id.uuid},{"slot",x.id.slot}}},{"voltage_controls",{{"kind","switch_delay"},{"identity",{{"slot",x.id.slot},{"architecture",x.arch},{"device_id",x.id.nvapi_devid},{"subsystem_id",x.id.nvapi_subsys}}},{"rows",ProfileJson::array({{{"index",index},{"type",3},{"voltage_domain",1},{"operation_type",1},{"i2c_device_index",index+2},{"switch_delay_us",value}}})}}}};}
void ordered_composition(){CatalogFake x;x.admission=false;auto backend=client(x);
    const auto doc=compose_profiles({{"Delay",delays(x,0,80)},{"Requests",requests(x,{{0,5000},{1,-2000}})},{"Admission",gate(x,true)}});
    check(execute(*backend,doc,true).ok&&x.trace.empty()&&!x.admission,"projected admission whole preview issues no writes");
    check(execute(*backend,doc).ok&&x.admission&&x.value(0,0)==5000&&x.value(0,1)==-2000&&word(x.device_control,12)==80,"mixed native/Python catalog applies all selected values");
    check(x.trace==std::vector<std::string>({"gate:on","requests","requests","delay"}),"gate enables before selected request writes and independent delay");
    x.trace.clear();check(backend->execute(undo()).ok&&!x.admission&&x.value(0,0)==1000&&x.value(0,1)==1111&&word(x.device_control,12)==20,"grouped Undo restores selected originals");
    check(!x.trace.empty()&&x.trace.back()=="gate:off"&&!backend->read(false).undo_available,"admission restores last after dependent requests");backend->close();
}
void preflight_and_partial_failure(){CatalogFake x;x.admission=false;auto backend=client(x);auto bad=delays(x,1,90);bad["voltage_controls"]["rows"][0]["i2c_device_index"]=99;
    check(!execute(*backend,compose_profiles({{"Gate",gate(x,true)},{"Requests",requests(x,{{0,5000}})},{"Bad delay",bad}})).ok&&x.trace.empty(),"later device mismatch prevents even admission SET");
    x.exports=false;check(!execute(*backend,compose_profiles({{"Gate",gate(x,true)},{"Requests",requests(x,{{0,5000}})}})).ok&&x.trace.empty(),"missing public request export blocks projected admission transaction");x.exports=true;
    check(!execute(*backend,requests(x,{{0,5000}})).ok&&x.trace.empty(),"request profile never enables admission implicitly");
    const auto doc=compose_profiles({{"Gate",gate(x,true)},{"Requests",requests(x,{{0,5000}})},{"Delay",delays(x,0,80)}});x.fail_device_write=true;
    const auto failed=execute(*backend,doc);check(!failed.ok&&failed.may_have_changed&&x.admission&&backend->read(false).undo_available,"accepted failed later device write retains entire issued group");
    check(!backend->execute(undo()).ok&&backend->read(false).undo_available&&x.admission,"failed partial restoration retains group and admission for retry");x.blocked_device_reads=0;
    check(backend->execute(undo()).ok&&!x.admission&&x.value(0,0)==1000&&word(x.device_control,12)==20&&!backend->read(false).undo_available,"retry verifies completed siblings and restores remaining requests");backend->close();
}
void final_interference_and_history(){CatalogFake x;auto backend=client(x);x.interfere_on_gate_off=true;
    const auto doc=compose_profiles({{"Selected requests",requests(x,{{0,5000},{1,1111}})},{"Disable",gate(x,false)}});
    const auto result=execute(*backend,doc);check(!result.ok&&result.may_have_changed&&x.value(0,1)==9876&&backend->read(false).undo_available,"full final verification catches initially no-op selected field changed by later gate");
    x.trace.clear();check(backend->execute(undo()).ok&&x.admission&&x.value(0,0)==1000&&x.value(0,1)==9876,"Undo restores only issued fields and preserves external no-op replacement");
    check(!x.trace.empty()&&x.trace.front()=="gate:on","original admission restored before dependent Undo");
    check(execute(*backend,requests(x,{{0,5000}})).ok&&execute(*backend,requests(x,{{0,6000}})).ok,"consecutive catalog request profiles");
    check(backend->execute(undo()).ok&&x.value(0,0)==5000&&backend->execute(undo()).ok&&x.value(0,0)==1000,"older catalog Undo remains meaningful after latest restore");backend->close();
}
}
int main(){ordered_composition();preflight_and_partial_failure();final_interference_and_history();std::printf("Voltage catalog client: %d failures\n",failures);return failures?1:0;}
