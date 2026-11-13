// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// CoreClient integration with injectable transport only; no GPU access.
#include "core_client.h"
#include "profile_io.h"
#include <windows.h>
#include <array>
#include <cstdio>
#include <cstring>
using namespace druta::ui;
namespace {
int failures=0,reads=0,writes=0,table_reads=0;dc_gpu_identity id{};
std::array<uint8_t,0x708> info{};std::array<uint8_t,0x188> control{};
void check(bool b,const char* s){if(!b){++failures;std::printf("FAIL: %s\n",s);}}
void put(uint8_t* p,size_t at,uint32_t v){std::memcpy(p+at,&v,4);}uint32_t get(const uint8_t* p,size_t at){uint32_t v=0;std::memcpy(&v,p+at,4);return v;}
uint32_t DC_CALL version(){return DC_ABI_VERSION;}dc_status DC_CALL enumerate(dc_gpu_entry*,size_t,size_t* n){*n=0;return DC_OK;}
dc_status DC_CALL open_gpu(const char*,dc_gpu** out){*out=reinterpret_cast<dc_gpu*>(1);return DC_OK;}void DC_CALL close_gpu(dc_gpu*){}
dc_status DC_CALL identity(const dc_gpu*,dc_gpu_identity* out){*out=id;return DC_OK;}
dc_status DC_CALL lock(dc_gpu*){return DC_OK;}dc_status DC_CALL unlock(dc_gpu*){return DC_OK;}
int32_t DC_CALL supported(uint32_t a){return a>=4&&a<=10;}
dc_status DC_CALL bind(dc_gpu*,int32_t,dc_target* out,dc_error*){out->size=sizeof(*out);std::strcpy(out->slot,id.slot);out->architecture=7;out->handle=id.nvapi_handle;out->device_id=id.nvapi_devid;out->subsystem_id=id.nvapi_subsys;return DC_OK;}
dc_status DC_CALL transport(dc_gpu*,dc_rm_transport* out,dc_error*){out->captured=1;out->header_count=17;out->header[0]=0x4E564441;out->header[1]=0x10002;out->header[2]=76;out->header[3]=0x4E562A2A;out->header[4]=0x0500002B;out->header[15]=8;return DC_OK;}
dc_status DC_CALL escape(dc_gpu*,uint8_t* raw,size_t,const dc_escape_fields*,int32_t* nt,dc_error*){
 auto* p=reinterpret_cast<uint32_t*>(raw);auto* payload=reinterpret_cast<uint8_t*>(p+17);*nt=0;p[5]=p[11]=1;p[16]=0;
 switch(p[14]){case 0x2080B205:++reads;std::memcpy(payload,info.data(),info.size());break;
 case 0x2080B207:++reads;std::memcpy(payload,control.data(),control.size());break;
 case 0x2080F208:++writes;check(get(payload,4)==1,"singleton selected device mask");std::memcpy(control.data()+8,payload+8,12);break;
 default:++table_reads;p[16]=0x1F;break;}return DC_OK;
}
CoreApi api(){CoreApi a;a.abi_version=version;a.enumerate=enumerate;a.open=open_gpu;a.close=close_gpu;a.identity=identity;
 VoltagePolicyApi t;t.identity=identity;t.bind=bind;t.architecture_supported=supported;t.transport=transport;t.escape=escape;t.lock=lock;t.unlock=unlock;
 a.voltage_tables.transport=t;a.voltage_limits.transport=t;return a;}
#if defined(_MSC_VER)
#define NOINLINE __declspec(noinline)
#else
#define NOINLINE __attribute__((noinline))
#endif
NOINLINE void run(){id.size=sizeof(id);id.nvapi_ok=id.nvapi_selected=1;id.nvapi_handle=42;id.nvapi_devid=0x1234;id.nvapi_subsys=0x99887766;
 std::strcpy(id.slot,"0000:09:00.0");std::strcpy(id.uuid,"GPU-alt");info[0]=control[0]=1;put(info.data(),4,1);put(control.data(),4,1);
 info[8]=info[9]=control[8]=control[9]=3;info[10]=1;info[12]=1;put(info.data(),16,100);put(info.data(),20,200);put(control.data(),12,50);
 auto client=create_core_backend_for_test(api());std::string error;check(client->open(id.slot,error),"open alternate fake adapter");
 client->read(false);client->read(true);check(reads==0&&table_reads==0&&writes==0,"open and ordinary polling do not read or write voltage services");
 Command c;c.operation=Command::Operation::voltage_tables_read;c.voltage_families=2;client->execute(c);
 auto first=client->read(false);check(first.voltage_tables && first.voltage_limits && reads==0 && writes==0,"offset inspection leaves device family independent");
 c.operation=Command::Operation::voltage_limits_read;c.voltage_families=2;check(client->execute(c).ok,"explicit device family read routed");
 auto before=client->read(false);check(before.voltage_limits && before.voltage_limits->rows.size()==1 && before.voltage_limits->rows[0].value==50,"device row published");
 const auto old=before.voltage_limits;const auto row=old->rows[0];VoltageLimitRequest request{row.key,60,{},row.binding,row.value,{}};
 c={};c.operation=Command::Operation::voltage_limits_apply;c.voltage_limit_requests=std::make_shared<const std::vector<VoltageLimitRequest>>(std::vector<VoltageLimitRequest>{request});
 check(client->execute(c).ok&&writes==1&&get(control.data(),12)==60,"explicit selected apply routes one writer");
 auto after=client->read(false);check(old->rows[0].value==50 && after.voltage_limits->rows[0].value==60,"published model remains immutable");
 wchar_t dir[MAX_PATH]{},path[MAX_PATH]{};check(GetTempPathW(MAX_PATH,dir)&&GetTempFileNameW(dir,L"dvt",0,path),"temporary profile path");DeleteFileW(path);
 c={};c.operation=Command::Operation::voltage_limits_save;c.profile_path=path;c.voltage_limit_selection=std::make_shared<const std::vector<VoltageLimitKey>>(std::vector<VoltageLimitKey>{row.key});
 check(client->execute(c).ok,"save qualified typed selected request");try{auto doc=read_profile(path);check(doc["entries"].size()==1&&doc["entries"][0]["value"]==60&&!doc.contains("packet"),"profile contains selected typed value, no packet");}catch(...){check(false,"saved profile readable");}DeleteFileW(path);
 c.voltage_limit_selection=std::make_shared<const std::vector<VoltageLimitKey>>(std::vector<VoltageLimitKey>{{VoltageLimitKind::switch_delay,0,31}});
 check(!client->execute(c).ok&&GetFileAttributesW(path)==INVALID_FILE_ATTRIBUTES,"unqualified save creates no profile");
 client->close();check(client->open(id.slot,error),"reconnect fake adapter");client->read(true);check(writes==1,"close and reconnect never replay request");client->close();
}
}
int main(){run();std::printf("Voltage services integration: %d failures\n",failures);return failures?1:0;}
