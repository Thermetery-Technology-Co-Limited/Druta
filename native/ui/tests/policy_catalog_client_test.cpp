// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// One established packet fixture, real CoreClient catalog, no DLL/GPU/files.
#include "core_client.h"
#include "profile_catalog.h"
#define main policy_packet_fixture_entry
#include "voltage_policy_service_test.cpp"
#undef main
namespace {
std::vector<std::string> policy_trace;
bool fail_next_vf=false,interfere_on_gate_off=false;
uint32_t DC_CALL catalog_version(){return DC_ABI_VERSION;}
dc_status DC_CALL catalog_enumerate(dc_gpu_entry*,size_t,size_t* count){*count=0;return DC_OK;}
dc_status DC_CALL catalog_open(const char* slot,dc_gpu** out){if(std::strcmp(slot,dev.slot))return DC_E_ARGUMENT;*out=reinterpret_cast<dc_gpu*>(1);return DC_OK;}
void DC_CALL catalog_close(dc_gpu*){}
dc_status DC_CALL catalog_escape(dc_gpu* gpu,uint8_t* raw,size_t bytes,const dc_escape_fields* fields,int32_t* nt,dc_error* error){
    const auto* words=reinterpret_cast<const uint32_t*>(raw);const auto command=words[14];const bool gate_set=command==0x2080A031,vf_set=command==0x2080D01C;
    const bool disabling=gate_set&&words[18]==0;
    if(gate_set)policy_trace.push_back(disabling?"gate:off":"gate:on");
    if(vf_set){const auto* flags=raw+68;policy_trace.push_back("vf:"+std::to_string((flags[10]?2u:0u)|(flags[11]?4u:0u)));}
    const auto status=escape(gpu,raw,bytes,fields,nt,error);
    if(vf_set&&fail_next_vf){fail_next_vf=false;*nt=-1;blocked_get=4;}
    if(disabling&&interfere_on_gate_off){interfere_on_gate_off=false;header[10]=0;}
    return status;
}
CoreApi catalog_api(){CoreApi a;a.abi_version=catalog_version;a.enumerate=catalog_enumerate;a.open=catalog_open;a.close=catalog_close;a.identity=identity;a.lock=lock;a.unlock=unlock;a.voltage_policy=api();a.voltage_policy.escape=catalog_escape;return a;}
Command catalog_command(const ProfileJson& doc,bool preview=false){Command c;c.operation=preview?Command::Operation::preflight_catalog_profile:Command::Operation::apply_catalog_profile;c.catalog_profile=std::make_shared<const ProfileJson>(doc);return c;}
Command policy_command(Command::Operation op,VoltagePolicyRequest request={}){Command c;c.operation=op;c.voltage_policy=std::make_shared<const VoltagePolicyRequest>(request);return c;}
ProfileJson policy_document(uint32_t selection,uint32_t values){return {{"format","druta-native-voltage-policy"},{"version",1},{"uuid",dev.uuid},{"identity",{{"slot",dev.slot},{"architecture",7},{"device_id",dev.nvapi_devid},{"subsystem_id",dev.nvapi_subsys}}},{"selection",selection},{"values",values}};}
ProfileJson python_flags(bool monotonicity,bool smoothing){const auto identity=policy_document(6,0)["identity"];return {{"schema",2},{"scope","clock_vf"},{"device",{{"uuid",dev.uuid},{"slot",dev.slot}}},{"incomplete",ProfileJson::array()},{"clock_vf",{{"identity",identity},{"policies",{{"clock_vf.monotonicity",monotonicity},{"clock_vf.smoothening",smoothing}}}}}};}
ProfileJson python_gate(bool enabled){return {{"schema",2},{"scope","voltage_controls"},{"device",{{"uuid",dev.uuid},{"slot",dev.slot}}},{"voltage_controls",{{"kind","gate"},{"identity",policy_document(1,0)["identity"]},{"rows",ProfileJson::array({{{"enabled",enabled}}})}}}};}
void reset_catalog(){fresh();policy_trace.clear();fail_next_vf=interfere_on_gate_off=false;}
std::unique_ptr<IBackend> opened_catalog(){auto client=create_core_backend_for_test(catalog_api());std::string why;CHECK(client->open(dev.slot,why));return client;}
Command undo_command(){Command c;c.operation=Command::Operation::undo;return c;}
#if defined(_MSC_VER)
#define CATALOG_NOINLINE __declspec(noinline)
#else
#define CATALOG_NOINLINE __attribute__((noinline))
#endif
CATALOG_NOINLINE void coalescing_and_order(){reset_catalog();auto client=opened_catalog();
    const auto vf=python_flags(false,false);const auto info=inspect_profile(vf);CHECK(info.valid&&info.scope==ProfileScope::voltage_policy&&info.targets.size()==2);
    const auto monotonicity=select_profile_targets(vf,{info.targets[0]});const auto smoothing=select_profile_targets(vf,{info.targets[1]});
    // File order is intentionally unrelated to execution dependency order.
    const auto combined=compose_profiles({{"Monotonicity",monotonicity},{"Gate",policy_document(1,1)},{"Smoothing",smoothing}});
    CHECK(client->execute(catalog_command(combined,true)).ok&&writes==0&&policy_trace.empty()&&!client->read(false).undo_available);
    CHECK(client->execute(catalog_command(combined)).ok&&gate&&header[10]==0&&header[11]==0);
    CHECK(policy_trace==std::vector<std::string>({"gate:on","vf:0"})); // one coalesced VF write
    CHECK(client->read(false).undo_available);policy_trace.clear();CHECK(client->execute(undo_command()).ok&&!gate&&header[10]==1&&header[11]==1);
    CHECK(policy_trace==std::vector<std::string>({"vf:6","gate:off"})&&!client->read(false).undo_available);
    const auto current_writes=writes;CHECK(client->execute(catalog_command(compose_profiles({{"VF",python_flags(true,true)},{"Gate",python_gate(false)}}))).ok&&writes==current_writes&&!client->read(false).undo_available);
    client->close();
}
CATALOG_NOINLINE void disable_last_and_original_undo(){reset_catalog();gate=true;auto client=opened_catalog();
    // Keep a meaningful older standalone VF Undo behind the catalog group.
    CHECK(client->execute(policy_command(Command::Operation::voltage_policy_apply,{4,0,4})).ok&&header[11]==0);policy_trace.clear();
    const auto combined=compose_profiles({{"Gate disable",python_gate(false)},{"VF",policy_document(6,2)}});
    CHECK(client->execute(catalog_command(combined)).ok&&!gate&&header[10]==1&&header[11]==0&&policy_trace==std::vector<std::string>({"gate:off"}));
    // A no-op VF component does not invent a second receipt.
    policy_trace.clear();CHECK(client->execute(undo_command()).ok&&gate&&header[11]==0&&policy_trace==std::vector<std::string>({"gate:on"}));
    CHECK(client->execute(policy_command(Command::Operation::voltage_policy_undo)).ok&&header[11]==1);
    // Now both transactions change: disable remains last; original admission
    // is restored before the VF receipt during grouped Undo.
    policy_trace.clear();const auto changed=compose_profiles({{"Gate disable",policy_document(1,0)},{"VF",python_flags(false,false)}});
    CHECK(client->execute(catalog_command(changed)).ok&&policy_trace==std::vector<std::string>({"vf:0","gate:off"}));
    policy_trace.clear();CHECK(client->execute(undo_command()).ok&&gate&&header[10]==1&&header[11]==1&&policy_trace==std::vector<std::string>({"gate:on","vf:6"}));client->close();
}
CATALOG_NOINLINE void whole_preflight_and_identity(){reset_catalog();auto client=opened_catalog();const auto vf=policy_document(6,0);auto wrong=policy_document(1,1);wrong["identity"]["device_id"]=dev.nvapi_devid+1;
    CHECK(!client->execute(catalog_command(compose_profiles({{"VF first",vf},{"Wrong adapter later",wrong}}))).ok&&writes==0&&!client->read(false).undo_available);
    unsupported_gate=true;CHECK(!client->execute(catalog_command(compose_profiles({{"VF first",vf},{"Unsupported later",python_gate(true)}}))).ok&&writes==0&&!client->read(false).undo_available);unsupported_gate=false;
    auto foreign=vf;foreign["uuid"]="GPU-other";CHECK(!client->execute(catalog_command(foreign)).ok&&writes==0);
    auto bad=vf;bad["selection"]=7;CHECK(!client->execute(catalog_command(bad)).ok&&writes==0);
    client->close();
}
CATALOG_NOINLINE void uncertain_launched_receipts(){reset_catalog();auto client=opened_catalog();const auto combined=compose_profiles({{"Gate",python_gate(true)},{"VF",python_flags(false,false)}});fail_next_vf=true;
    const auto failed=client->execute(catalog_command(combined));CHECK(!failed.ok&&failed.may_have_changed&&writes==2&&gate&&header[10]==0&&header[11]==0);
    CHECK(client->read(false).undo_available);blocked_get=0;policy_trace.clear();CHECK(client->execute(undo_command()).ok&&!gate&&header[10]==1&&header[11]==1);
    CHECK(policy_trace==std::vector<std::string>({"vf:6","gate:off"})&&!client->read(false).undo_available);client->close();
}
CATALOG_NOINLINE void final_noop_interference(){reset_catalog();gate=true;auto client=opened_catalog();
    // Monotonicity is selected but initially already matches. Gate's later
    // write changes it after the VF SET; whole final verification must notice.
    const auto combined=compose_profiles({{"Gate",python_gate(false)},{"VF",python_flags(true,false)}});interfere_on_gate_off=true;
    const auto failed=client->execute(catalog_command(combined));CHECK(!failed.ok&&failed.may_have_changed&&!gate&&header[10]==0&&header[11]==0&&client->read(false).undo_available);
    CHECK(client->execute(undo_command()).ok&&gate&&header[11]==1&&header[10]==0); // foreign no-op field is not overwritten
    client->close();
}
}
int main(){coalescing_and_order();disable_last_and_original_undo();whole_preflight_and_identity();uncertain_launched_receipts();final_noop_interference();std::printf("Policy catalog client: %u checks\n",checks);return 0;}
