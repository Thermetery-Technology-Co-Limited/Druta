// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Real CoreClient with the existing injected policy packet fixture; no DLL/GPU.
#include "core_client.h"
#define main policy_service_fixture_entry
#include "voltage_policy_service_test.cpp"
#undef main
#include <chrono>
#include <filesystem>
namespace {
uint32_t DC_CALL client_version(){return DC_ABI_VERSION;}
dc_status DC_CALL client_enumerate(dc_gpu_entry* entries,size_t capacity,size_t* count){*count=1;if(!entries||!capacity)return DC_E_BUFFER;entries[0]={};entries[0].size=sizeof(entries[0]);std::strcpy(entries[0].slot,dev.slot);std::strcpy(entries[0].uuid,dev.uuid);std::strcpy(entries[0].name,"Injected policy adapter");entries[0].has_nvapi=entries[0].has_devid=1;entries[0].devid=dev.nvapi_devid;entries[0].subsys=dev.nvapi_subsys;return DC_OK;}
dc_status DC_CALL client_open(const char* slot,dc_gpu** out){if(std::strcmp(slot,dev.slot))return DC_E_ARGUMENT;*out=reinterpret_cast<dc_gpu*>(1);return DC_OK;}
void DC_CALL client_close(dc_gpu*){}
CoreApi client_api(){CoreApi a;a.abi_version=client_version;a.enumerate=client_enumerate;a.open=client_open;a.close=client_close;a.identity=identity;a.voltage_policy=api();return a;}
struct OwnedFiles {
    std::filesystem::path directory;
    OwnedFiles(){directory=std::filesystem::temp_directory_path()/("druta-policy-profile-client-"+std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));CHECK(std::filesystem::create_directory(directory));}
    ~OwnedFiles(){std::error_code error;std::filesystem::remove(directory/"selected.json",error);std::filesystem::remove(directory/"gate.json",error);std::filesystem::remove(directory,error);}
};
Command command(Command::Operation operation,const VoltagePolicyRequest& request){Command c;c.operation=operation;c.voltage_policy=std::make_shared<const VoltagePolicyRequest>(request);return c;}
#if defined(_MSC_VER)
#define POLICY_NOINLINE __declspec(noinline)
#else
#define POLICY_NOINLINE __attribute__((noinline))
#endif
POLICY_NOINLINE void run(){fresh();OwnedFiles files;auto client=create_core_backend_for_test(client_api());CHECK(client->enumerate().size()==1);std::string reason;CHECK(client->open(dev.slot,reason));CHECK(writes==0&&transport_calls==0);
    // First operation may be an already-matching gate request: still binds
    // current metadata, but must neither SET nor fabricate an Undo owner.
    CHECK(client->execute(command(Command::Operation::voltage_policy_apply,{1,0,{}})).ok&&writes==0);
    auto first=client->read(false);CHECK(first.voltage_policies&&first.voltage_policies->target&&first.voltage_policies->rows[0].current==false&&!first.voltage_policies->undo_available);
    CHECK(client->execute(command(Command::Operation::voltage_policy_read,{7,0,{}})).ok);auto current=client->read(false);const auto immutable=current.voltage_policies;
    // Save must capture new live values, not the previously published snapshot.
    header[10]=0;Command save=command(Command::Operation::voltage_policy_save,{6,0,{}});save.profile_path=(files.directory/"selected.json").wstring();CHECK(client->execute(save).ok&&writes==0);
    const auto saved=read_profile(save.profile_path);CHECK(saved.at("selection")==6&&saved.at("values")==4);CHECK(immutable->rows[1].current==true);current=client->read(false);CHECK(current.voltage_policies->rows[1].current==false&&immutable->rows[1].current==true);
    auto desired=saved;desired["values"]=0;const auto staged=stage_voltage_policy_profile(desired,*current.voltage_policies);CHECK(staged.selection==6&&staged.expected==4&&writes==0);
    const uint8_t opaque=header[0x20];CHECK(client->execute(command(Command::Operation::voltage_policy_apply,staged)).ok&&writes==1&&header[10]==0&&header[11]==0&&header[0x20]==opaque);
    const auto still_old=current.voltage_policies;current=client->read(false);CHECK(still_old->rows[2].current==true&&current.voltage_policies->rows[2].current==false);
    const auto no_op=stage_voltage_policy_profile(desired,*current.voltage_policies);CHECK(client->execute(command(Command::Operation::voltage_policy_apply,no_op)).ok&&writes==1&&client->read(false).voltage_policies->undo_available);
    // A loaded expected state is immutable; later live change must refuse even
    // when the desired target happens to equal the new selected value.
    auto stale_document=desired;stale_document["selection"]=2;stale_document["values"]=2;const auto stale=stage_voltage_policy_profile(stale_document,*current.voltage_policies);header[10]=1;const auto previous=writes;CHECK(!client->execute(command(Command::Operation::voltage_policy_apply,stale)).ok&&writes==previous);
    save.voltage_policy=std::make_shared<const VoltagePolicyRequest>(VoltagePolicyRequest{7,0,{}});CHECK(!client->execute(save).ok&&read_profile(save.profile_path)==saved&&writes==previous);
    // Unsupported admission is independent of supported VF capture.
    unsupported_gate=true;save.voltage_policy=std::make_shared<const VoltagePolicyRequest>(VoltagePolicyRequest{2,0,{}});CHECK(client->execute(save).ok);const auto selected=read_profile(save.profile_path);CHECK(selected.at("selection")==2&&selected.at("values")==2);
    save.voltage_policy=std::make_shared<const VoltagePolicyRequest>(VoltagePolicyRequest{1,0,{}});save.profile_path=(files.directory/"gate.json").wstring();CHECK(!client->execute(save).ok&&!std::filesystem::exists(save.profile_path));unsupported_gate=false;
    // Runtime matching UUID absence still works; files require a UUID.
    client->close();dev.uuid[0]=0;CHECK(client->open(dev.slot,reason));CHECK(client->execute(command(Command::Operation::voltage_policy_read,{1,0,{}})).ok);CHECK(!client->execute(save).ok&&!std::filesystem::exists(save.profile_path));
    const auto before=writes;client->close();CHECK(client->open(dev.slot,reason));client->read(false);CHECK(writes==before);client->close();
}
}
int main(){run();std::printf("Policy profile client: %u checks\n",checks);return 0;}
