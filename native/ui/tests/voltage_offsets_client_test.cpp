// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "core_client.h"
#include "voltage_offsets_profile.h"
#include "profile_catalog.h"
#include <windows.h>
#define main unused_offset_service_fixture_main
#include "voltage_offsets_service_test.cpp"
#undef main
namespace {
uint32_t DC_CALL client_version(){return DC_ABI_VERSION;}
dc_status DC_CALL client_enumerate(dc_gpu_entry* rows,size_t capacity,size_t* count){*count=1;if(!rows||!capacity)return DC_E_BUFFER;rows[0]={};rows[0].size=sizeof(rows[0]);std::strcpy(rows[0].slot,identity.slot);std::strcpy(rows[0].uuid,identity.uuid);rows[0].has_nvapi=rows[0].has_devid=1;rows[0].devid=identity.nvapi_devid;rows[0].subsys=identity.nvapi_subsys;return DC_OK;}
dc_status DC_CALL client_open(const char* slot,dc_gpu** gpu){if(std::strcmp(slot,identity.slot))return DC_E_ARGUMENT;*gpu=reinterpret_cast<dc_gpu*>(1);return DC_OK;}
void DC_CALL client_close(dc_gpu*){}
std::unique_ptr<IBackend> client(){const auto offset=api();CoreApi a;a.abi_version=client_version;a.enumerate=client_enumerate;a.open=client_open;a.close=client_close;a.identity=id;a.lock=lock;a.unlock=unlock;a.voltage_tables.transport=offset.transport.transport;a.voltage_tables.offsets_api=offset;auto result=create_core_backend_for_test(a);std::string message;CHECK(result->open(identity.slot,message)&&sets==0);return result;}
Command command(Command::Operation op,uint32_t families=2){Command c;c.operation=op;c.voltage_families=families;return c;}
Command catalog(const ProfileJson& doc,bool preview=false){Command c;c.operation=preview?Command::Operation::preflight_catalog_profile:Command::Operation::apply_catalog_profile;c.catalog_profile=std::make_shared<const ProfileJson>(doc);return c;}
ActionResult execute(IBackend& backend,const Command& c){const auto result=backend.execute(c);if(!result.ok)std::fprintf(stderr,"Offset client outcome: %s\n",result.message.c_str());return result;}
void client_tests(){reset();auto backend=client();CHECK(execute(*backend,command(Command::Operation::voltage_tables_read)).ok&&sets==0);auto data=backend->read(false).voltage_tables;CHECK(data&&data->offsets_recognized&&data->rows.size()==2&&data->rows[0].key.family==VoltageTableFamily::offsets&&data->rows[0].key.field==2&&data->rows[0].writable);
 const auto document=capture_voltage_offsets_profile(*data,{data->rows[0].key});wchar_t temp[MAX_PATH]{},path[MAX_PATH]{};CHECK(GetTempPathW(MAX_PATH,temp)&&GetTempFileNameW(temp,L"dvo",0,path));auto save=command(Command::Operation::voltage_tables_save);save.profile_path=path;save.voltage_table_selection=std::make_shared<const std::vector<VoltageTableKey>>(std::vector<VoltageTableKey>{data->rows[0].key});CHECK(execute(*backend,save).ok&&sets==0);CHECK(read_profile(path)==document);CHECK(DeleteFileW(path));
 auto changed=document;changed["fields"][0]["value_uv"]=-5000;auto apply=command(Command::Operation::voltage_tables_apply);apply.voltage_table_requests=std::make_shared<const std::vector<VoltageTableRequest>>(stage_voltage_offsets_profile(changed,*data));CHECK(execute(*backend,apply).ok&&sets==1&&value(6)==-5000);
 const auto before=sets;Command default_undo;default_undo.operation=Command::Operation::voltage_tables_undo;CHECK(!backend->execute(default_undo).ok&&sets==before&&value(6)==-5000);CHECK(execute(*backend,command(Command::Operation::voltage_tables_undo)).ok&&value(6)==0&&sets==2);
 const ProfileJson python={{"schema",2},{"scope","voltage_offsets"},{"device",{{"uuid",identity.uuid},{"slot",identity.slot}}},{"voltage_offsets",{{"slot",identity.slot},{"values",{{"voltage.0.VMIN.offset",10000}}}}}};const auto combined=compose_profiles({{"Native OV",changed},{"Python VMIN",python}});const auto original=snapshot.equations.control;const auto first=sets;CHECK(execute(*backend,catalog(combined,true)).ok&&sets==first);CHECK(execute(*backend,catalog(combined)).ok&&sets==first+2&&value(6)==-5000&&value(7)==10000);Command undo;undo.operation=Command::Operation::undo;CHECK(execute(*backend,undo).ok&&sets==first+4&&snapshot.equations.control==original);
 auto foreign=changed;foreign["target"]["uuid"]="GPU-other";const auto unchanged=sets;CHECK(!backend->execute(catalog(foreign)).ok&&sets==unchanged);backend->close();CHECK(locks==0);
}
}
int main(){client_tests();std::printf("Voltage offsets CoreClient: %u checks\n",checks);return 0;}
