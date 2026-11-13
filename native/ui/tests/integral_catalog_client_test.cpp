// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Include only the original service fixture; no nested renamed-main includes.
#define main integral_catalog_fixture_entry
#include "power_integral_service_test.cpp"
#undef main
#include "core_client.h"
#include "profile_catalog.h"
namespace {
Fake* catalog_fixture=nullptr;
bool late_interference=false;int controls_after_two_sets=0;
uint32_t DC_CALL catalog_version(){return DC_ABI_VERSION;}
dc_status DC_CALL catalog_enumerate(dc_gpu_entry*,size_t,size_t* count){*count=0;return DC_OK;}
dc_status DC_CALL catalog_open(const char*,dc_gpu** out){*out=catalog_fixture->gpu();return DC_OK;}
void DC_CALL catalog_close(dc_gpu*){}
dc_status DC_CALL catalog_rm(dc_gpu* gpu,uint32_t command,const uint32_t* params,uint32_t param_count,int32_t has_mask,uint32_t mask_value,int32_t live,uint32_t* out,uint32_t capacity,uint32_t* count,dc_error* error){
    auto& fixture=fake(gpu);
    // Service second-SET readback and its final read succeed. Change the first
    // selected target only when the catalog performs its additional final
    // selected verification; this specifically exercises the host contract.
    if(late_interference&&fixture.sets==2&&command==0x2080A61A&&++controls_after_two_sets==3){put16(fixture.control,upper0,5500);late_interference=false;}
    return rm(gpu,command,params,param_count,has_mask,mask_value,live,out,capacity,count,error);
}
std::unique_ptr<IBackend> client(Fake& fixture){catalog_fixture=&fixture;fixture.id.nvapi_devid=fixture.target.device_id=0x1234;CoreApi core;core.abi_version=catalog_version;core.enumerate=catalog_enumerate;core.open=catalog_open;core.close=catalog_close;core.identity=identity;core.lock=lock;core.unlock=unlock;core.power_integral=api();core.power_integral.rm=catalog_rm;auto backend=create_core_backend_for_test(core);std::string error;check(backend->open(fixture.id.slot,error),"catalog client opens injected paired adapter");return backend;}
Command catalog_action(const ProfileJson& document,bool preflight=false){Command command;command.operation=preflight?Command::Operation::preflight_catalog_profile:Command::Operation::apply_catalog_profile;command.catalog_profile=std::make_shared<const ProfileJson>(document);return command;}
Command undo_catalog(){Command command;command.operation=Command::Operation::undo;return command;}
PowerIntegralData read_integral(IBackend& backend){Command command;command.operation=Command::Operation::power_integral_read;check(backend.execute(command).ok,"explicit integral read");const auto data=backend.read(false).power_integral;check(bool(data),"typed integral data published");return data?*data:PowerIntegralData{};}
ProfileJson python_bounds(const Fake& f,uint32_t policy,const ProfileJson& values){return python_profile(f,ProfileJson::array({{{"policy",policy},{"type",policy+5},{"channel",policy+10},{"unit",policy==0?"mW":"mA"},{"values",values}}}));}
ProfileJson native_upper(const PowerIntegralData& data,uint32_t policy,uint16_t value){auto result=capture_power_integral_profile(data,{{policy,PowerIntegralField::upper}});result["fields"][0]["raw"]=value;return result;}
ProfileJson raw_collection(const std::vector<ProfileComponent>& children,bool python=false){ProfileJson components=ProfileJson::array();for(const auto& c:children)components.push_back({{"name",c.name},{"state",c.document}});if(python)return {{"schema",2},{"scope","combined"},{"saved_at","2026-11-13"},{"saved_ts",1},{"components",components}};return {{"format","druta-native-combined"},{"version",1},{"components",components}};}
void coalesced_bounds(){
    Fake fixture;auto backend=client(fixture);const auto data=read_integral(*backend);const auto lower=python_bounds(fixture,0,{{"lower_raw",5000}}),upper=native_upper(data,0,6000);
    check(!backend->execute(catalog_action(lower,true)).ok&&fixture.sets==0,"lower-only child invalid against current upper alone");auto composed=compose_profiles({{"Lower Python",lower},{"Upper native",upper}});
    check(backend->execute(catalog_action(composed,true)).ok&&fixture.sets==0,"mixed native/Python disjoint bounds coalesce before whole-range preflight");check(backend->execute(catalog_action(composed)).ok&&fixture.sets==2&&get16(fixture.control,lower0)==5000&&get16(fixture.control,upper0)==6000,"catalog applies safe upper-first coalesced range");
    check(backend->read(false).undo_available,"catalog publishes aggregate Undo");check(backend->execute(undo_catalog()).ok&&get16(fixture.control,lower0)==1024&&get16(fixture.control,upper0)==4096&&!backend->read(false).undo_available,"grouped catalog Undo restores exact initial bounds");
    auto upper_python=python_bounds(fixture,0,{{"upper_raw",6000}});auto python_combined=raw_collection({{"Lower",lower},{"Upper",upper_python}},true);check(backend->execute(catalog_action(python_combined,true)).ok&&backend->execute(catalog_action(python_combined)).ok,"released Python combined form coalesces disjoint bounds");check(backend->execute(undo_catalog()).ok,"Python combined grouped Undo");
    auto duplicate=raw_collection({{"Lower1",lower},{"Lower2",lower}});const auto before=fixture.sets;check(!backend->execute(catalog_action(duplicate)).ok&&fixture.sets==before,"duplicate target refused before first SET");
    auto bad=upper;bad["fields"][0]["channel"]=77;check(!backend->execute(catalog_action(raw_collection({{"Lower",lower},{"Changed metadata",bad}}))).ok&&fixture.sets==before,"later metadata mismatch blocks every earlier selected write");bad=upper;bad["target"]["uuid"]="GPU-other";check(!backend->execute(catalog_action(raw_collection({{"Lower",lower},{"Wrong GPU",bad}}))).ok&&fixture.sets==before,"changed component identity blocks all writes");
    ProfileJson unavailable_boost={{"format","druta-native-daily"},{"version",1},{"target",{{"uuid",fixture.id.uuid},{"slot",fixture.id.slot}}},{"controls",{{"boost",{{"value",50},{"unit","%"}}}}}};auto invalid_other=compose_profiles({{"Integral",upper},{"Unavailable boost",unavailable_boost}});check(!backend->execute(catalog_action(invalid_other)).ok&&fixture.sets==before,"other component fresh preflight failure prevents integral SET");backend->close();
}
void partial_and_interference(){
    Fake fixture;auto backend=client(fixture);const auto data=read_integral(*backend);const auto first=native_upper(data,0,6000),second=python_bounds(fixture,1,{{"upper_raw",7000}});auto document=compose_profiles({{"First",first},{"Second",second}});fixture.fail_set_at=2;
    const auto failed=backend->execute(catalog_action(document));check(!failed.ok&&failed.may_have_changed&&fixture.sets==2&&backend->read(false).undo_available,"accepted failed second SET retains aggregate launched-field Undo");fixture.fail_set_at=0;check(backend->execute(undo_catalog()).ok&&get16(fixture.control,upper0)==4096&&get16(fixture.control,0x14+0xC4+14)==4096,"catalog Undo recovers all potentially launched integral fields");backend->close();
    Fake interference;backend=client(interference);const auto fresh=read_integral(*backend);document=compose_profiles({{"First",native_upper(fresh,0,6000)},{"Second",python_bounds(interference,1,{{"upper_raw",7000}})}});late_interference=true;controls_after_two_sets=0;const auto result=backend->execute(catalog_action(document));check(!result.ok&&result.may_have_changed&&controls_after_two_sets>=3&&backend->read(false).undo_available,"catalog final selected verification catches post-service interference");const auto before=interference.sets;check(!backend->execute(undo_catalog()).ok&&interference.sets==before,"grouped Undo refuses foreign selected replacement");put16(interference.control,upper0,6000);check(backend->execute(undo_catalog()).ok&&get16(interference.control,upper0)==4096&&get16(interference.control,0x14+0xC4+14)==4096,"retained aggregate receipt recovers when selected state again matches a qualified candidate");backend->close();late_interference=false;
}
void prior_undo(){
    Fake fixture;auto backend=client(fixture);auto data=read_integral(*backend);auto first=native_upper(data,0,5000);check(backend->execute(catalog_action(first)).ok,"first native catalog profile");data=read_integral(*backend);const auto before=fixture.sets;check(backend->execute(catalog_action(native_upper(data,0,5000))).ok&&fixture.sets==before,"catalog no-op does not write or erase prior Undo");auto wrong=native_upper(data,0,6000);wrong["fields"][0]["unit"]=1;check(!backend->execute(catalog_action(wrong)).ok&&fixture.sets==before,"failed next metadata preflight preserves earlier catalog Undo");check(backend->execute(catalog_action(native_upper(data,0,6000))).ok,"consecutive confirmed catalog profile");check(backend->execute(undo_catalog()).ok&&get16(fixture.control,upper0)==5000,"latest grouped Undo returns immediate previous profile");check(backend->execute(undo_catalog()).ok&&get16(fixture.control,upper0)==4096&&!backend->read(false).undo_available,"previous meaningful catalog Undo survives no-op, rejection and later profile");backend->close();
}
}
int main(){coalesced_bounds();partial_and_interference();prior_undo();std::printf("Integral catalog CoreClient: %d failures\n",failures);return failures?1:0;}
