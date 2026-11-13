// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// CoreClient catalog path with injectable transport; no GPU access.
#include "core_client.h"
#include "power_saving_fake.h"
#define main clock_fixture_entry
#include "clock_gating_service_test.cpp"
#undef main
#include "profile_catalog.h"
namespace {
int failures=0;power_saving_test::Fake* active=nullptr;
void check(bool ok,const char* why){if(!ok){++failures;std::printf("FAIL: %s\n",why);}}
uint32_t DC_CALL core_version(){return DC_ABI_VERSION;}
dc_status DC_CALL core_enumerate(dc_gpu_entry*,size_t,size_t* n){*n=0;return DC_OK;}
dc_status DC_CALL core_open(const char*,dc_gpu** out){*out=active->gpu();return DC_OK;}
void DC_CALL core_close(dc_gpu*){}
CoreApi client_api(){CoreApi a;a.abi_version=core_version;a.enumerate=core_enumerate;a.open=core_open;a.close=core_close;a.identity=power_saving_test::identity;a.lock=power_saving_test::lock;a.unlock=power_saving_test::unlock;a.power_saving=power_saving_test::api();a.clock_gating=::api();return a;}
#if defined(_MSC_VER)
#define NOINLINE __declspec(noinline)
#else
#define NOINLINE __attribute__((noinline))
#endif
bool fail_pcie=false,interfere_noop=false;
dc_status DC_CALL catalog_escape(dc_gpu* gpu,uint8_t* raw,size_t size,const dc_escape_fields* fields,int32_t* nt,dc_error* error){
 auto* p=reinterpret_cast<uint32_t*>(raw);const auto cmd=p[14];const auto status=escape(gpu,raw,size,fields,nt,error);
 if(cmd==0x20801805){if(interfere_noop)active->raw[0]=3500;if(fail_pcie){fail_pcie=false;*nt=-1;}}
 return status;
}
Command catalog(const ProfileJson& doc,bool preview=false){Command c;c.operation=preview?Command::Operation::preflight_catalog_profile:Command::Operation::apply_catalog_profile;c.catalog_profile=std::make_shared<const ProfileJson>(doc);return c;}
NOINLINE void catalog_tests(){
 fresh();power_saving_test::Fake f;active=&f;device=f.identity;auto a=client_api();a.clock_gating.transport.escape=catalog_escape;a.clock_gating.transport.bind=power_saving_test::bind;
 auto client=create_core_backend_for_test(a);std::string error;check(client->open(f.identity.slot,error),"open catalog fake");client->read(false);check(f.sets==0&&pcie_sets==0&&f.gets==0&&bind_calls==0,"open and polling do not apply or inspect saving controls");
 Command c;c.operation=Command::Operation::power_saving_read;c.saving_families=2|4;check(client->execute(c).ok,"capture individual and idle");
 c={};c.operation=Command::Operation::clock_gating_read;c.clock_families=8;check(client->execute(c).ok,"capture PCIe");auto data=client->read(false);
 auto power=capture_power_saving_profile(*data.power_saving,{{PowerSavingFamily::individual,0},{PowerSavingFamily::idle_threshold,0}});
 for(auto& row:power["requests"]){if(row["family"]==1)row["value"]=1;else row["value"]=4000;}
 auto clock=capture_clock_gating_profile(*data.clock_gating,{ClockGatingFamily::pcie});clock["entries"][0]["generation"]=3;
 auto pyidentity=ProfileJson{{"slot",f.identity.slot},{"architecture",7},{"device_id",f.identity.nvapi_devid},{"subsystem_id",f.identity.nvapi_subsys}};
 auto pylow=ProfileJson{{"schema",2},{"scope","lowpower_requests"},{"device",{{"uuid",f.identity.uuid},{"slot",f.identity.slot}}},{"lowpower_requests",{{"identity",pyidentity},{"kind","individual"},{"index",0},{"mode","disabled"}}}};
 auto pypcie=ProfileJson{{"schema",2},{"scope","pcie_speed"},{"saved_at","2026-11-13"},{"saved_ts",1},{"device",{{"name","alternate"},{"vbios",nullptr},{"driver",nullptr},{"uuid",f.identity.uuid},{"slot",f.identity.slot}}},{"incomplete",ProfileJson::array()},{"pcie_speed",{{"identity",pyidentity},{"target_generation",3}}}};
 check(client->execute(catalog(compose_profiles({{"Python individual",pylow},{"Python PCIe",pypcie}}),true)).ok&&f.sets==0&&pcie_sets==0,"Python mixed profile preflight has no writes");
 auto combined=compose_profiles({{"Power",power},{"PCIe",clock}});const auto initial_sets=f.sets,initial_pcie=pcie_sets;
 check(client->execute(catalog(combined,true)).ok&&f.sets==initial_sets&&pcie_sets==initial_pcie,"whole preflight is read-only");
 auto result=client->execute(catalog(combined));check(result.ok&&f.reasons[0]==1&&f.raw[0]==4000&&pcie_target==3,"mixed selected catalog applies");
 Command undo;undo.operation=Command::Operation::undo;check(client->execute(undo).ok&&f.reasons[0]==0&&f.raw[0]==2000&&pcie_target==1,"grouped Undo restores each original");
 root_max=2;const auto before=f.sets,before_pcie=pcie_sets;check(!client->execute(catalog(combined)).ok&&f.sets==before&&pcie_sets==before_pcie,"later PCIe capability failure prevents earlier writes");root_max=4;
 fail_pcie=true;result=client->execute(catalog(combined));check(!result.ok&&result.may_have_changed&&client->read(false).undo_available,"accepted failed later PCIe retains grouped Undo");check(client->execute(undo).ok&&f.reasons[0]==0&&f.raw[0]==2000&&pcie_target==1,"grouped Undo recovers issued prefix and PCIe");
 auto noop=power;for(auto& row:noop["requests"])if(row["family"]==2)row["value"]=2000;auto noops=compose_profiles({{"Power",noop},{"PCIe",clock}});interfere_noop=true;result=client->execute(catalog(noops));check(!result.ok&&result.may_have_changed&&client->read(false).undo_available,"final verification detects selected no-op interference");interfere_noop=false;const auto externally_changed=f.raw[0];check(client->execute(undo).ok&&f.raw[0]==externally_changed,"Undo preserves externally changed no-op");
 client->close();active=nullptr;
}
}
int main(){catalog_tests();std::printf("Saving catalog client: %d failures\n",failures);return failures?1:0;}
