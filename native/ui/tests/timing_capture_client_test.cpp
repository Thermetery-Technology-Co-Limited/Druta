// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "core_client.h"
#include "timing_model.h"
#include <windows.h>
#include <cstdio>
#include <cstring>
using namespace druta::ui;
namespace {
int failures=0,sets=0,clears=0;bool held=false,foreign=false,fail_release=false,at_p0=true,no_uuid=false;int identity_calls=0,appear_at=0;uint32_t uv=0,domain=0;
void check(bool b,const char* s){if(!b){++failures;std::printf("FAIL: %s\n",s);}}
uint32_t DC_CALL version(){return DC_ABI_VERSION;}dc_status DC_CALL enumerate(dc_gpu_entry*,size_t,size_t* n){*n=0;return DC_OK;}
dc_status DC_CALL open_gpu(const char*,dc_gpu** g){*g=reinterpret_cast<dc_gpu*>(1);return DC_OK;}void DC_CALL close_gpu(dc_gpu*){}
dc_status DC_CALL identity(const dc_gpu*,dc_gpu_identity* d){d->nvapi_ok=d->nvapi_selected=1;d->nvapi_handle=42;std::strcpy(d->slot,"0000:09:00.0");++identity_calls;if(!no_uuid || (appear_at && identity_calls>=appear_at))std::strcpy(d->uuid,"GPU-alt");return DC_OK;}
dc_status DC_CALL lock(dc_gpu*){return DC_OK;}dc_status DC_CALL unlock(dc_gpu*){return DC_OK;}
dc_status DC_CALL telemetry(dc_gpu*,dc_telemetry* t){t->present=DC_T_MEM|DC_T_PSTATE|DC_T_MEM_OFF;t->mem=at_p0||held?7000:400;t->pstate=at_p0||held?0:8;t->mem_off=0;return DC_OK;}
dc_status DC_CALL caps(dc_gpu*,dc_capabilities* c){c->mem_clock_count=1;c->mem_clocks[0]=7000;return DC_OK;}
dc_status DC_CALL vf_status(dc_gpu*,int32_t,int64_t,dc_vf_lock_status* s){*s={};s->locked=held;s->lock={domain,3,uv,1};return DC_OK;}
dc_status DC_CALL clock_status(dc_gpu*,dc_clk_lock_status* s){*s={};s->locked=foreign;return DC_OK;}
dc_status DC_CALL vf_set(dc_gpu*,int64_t v,int32_t,int64_t d,dc_outcome* o){++sets;held=true;uv=static_cast<uint32_t>(v);domain=static_cast<uint32_t>(d);o->ok=1;return DC_OK;}
dc_status DC_CALL vf_clear(dc_gpu*,int32_t,int64_t,int32_t,int64_t,dc_outcome* o){++clears;if(!fail_release){held=false;o->ok=1;}return DC_OK;}
int32_t DC_CALL pending(const dc_gpu*){return 0;}dc_status DC_CALL recover(dc_gpu*,dc_outcome* o){o->ok=1;return DC_OK;}
dc_status DC_CALL snapshot(dc_gpu*,dc_vf_delta_snapshot* s){s->freq_div=1;s->count=1;s->deltas[0]={3,0};return DC_OK;}
dc_status DC_CALL layout(dc_gpu*,int32_t,dc_vf_layout* l){l->n_entries=4;l->gpu_count=1;l->gpu_idx[0]=3;l->freq_div=1;return DC_OK;}
dc_status DC_CALL curve(dc_gpu*,dc_vf_curve* c){c->count=1;c->points[0]={3,900,1500,0};return DC_OK;}
CoreApi api(){CoreApi a;a.abi_version=version;a.enumerate=enumerate;a.open=open_gpu;a.close=close_gpu;a.identity=identity;a.telemetry=telemetry;a.capabilities=caps;a.lock=lock;a.unlock=unlock;a.snapshot=snapshot;a.curve_layout=layout;a.curve=curve;
 a.tuning.identity=identity;a.tuning.lock=lock;a.tuning.unlock=unlock;a.tuning.vf_status=vf_status;a.tuning.clock_status=clock_status;a.tuning.vf_set=vf_set;a.tuning.vf_clear=vf_clear;a.tuning.recovery_pending=pending;a.tuning.recover=recover;return a;}
#if defined(_MSC_VER)
#define NOINLINE __declspec(noinline)
#else
#define NOINLINE __attribute__((noinline))
#endif
NOINLINE void run(const wchar_t* path){auto b=create_core_backend_for_test(api());std::string why;check(b->open("0000:09:00.0",why),"open fake GPU");
 Command select;select.operation=Command::Operation::timings_select_helper;select.timing_helper_path=path;check(b->execute(select).ok,"select helper");
 Command c;c.operation=Command::Operation::timings_capture_p0;c.timing_generation=1;c.curve_cap_mv=1000;
 check(b->execute(c).ok && sets==0 && clears==0,"already P0 captures directly through retained helper pin");
 at_p0=false;check(b->execute(c).ok && sets==1 && clears==1 && !held,"idle capture acquires then releases own hold");
 foreign=true;check(!b->execute(c).ok && sets==1 && clears==1,"foreign lock refuses hold");foreign=false;
 fail_release=true;auto failed=b->execute(c);auto state=b->read(false);check(!failed.ok&&held&&state.timings&&state.timings->capture_hold_pending,"failed release retains published ownership");
 fail_release=false;Command release;release.operation=Command::Operation::timings_release_capture_hold;check(b->execute(release).ok&&!held,"explicit cleanup retry succeeds");
 b->close();check(b->open("0000:09:00.0",why)&&b->execute(select).ok,"reopen and select helper");at_p0=true;check(b->execute(c).ok,"reopen clears temporary owner");b->close();
 no_uuid=true;appear_at=0;identity_calls=0;auto no_id=create_core_backend_for_test(api());check(no_id->open("0000:09:00.0",why)&&no_id->execute(select).ok,"open NVAPI-only fake");
 check(no_id->execute(c).ok,"runtime P0 capture allows missing UUID");identity_calls=0;appear_at=3;check(!no_id->execute(c).ok,"UUID appearing during capture refuses changed identity");
 appear_at=0;no_id->close();no_uuid=false;}
}
int wmain(int argc,wchar_t** argv){if(argc>1){const std::wstring cmd=argv[1];if(cmd==L"fields"){std::printf("RC CONFIG0 [7:0] 255 Cycles\n");return 0;}
 if(cmd==L"dump"&&argc==5){std::printf("0000:09:00.0 TU104 (Turing)\n [broadcast] base 0x001000\n CONFIG0 @0x001000 = 0x00000014 (DOCUMENTED)\n");return 0;}
 if(cmd==L"save"&&argc==6){HANDLE f=CreateFileW(argv[5],GENERIC_WRITE,0,nullptr,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,nullptr);if(f==INVALID_HANDLE_VALUE)return 2;
 const char text[]="{\"slot\":\"0000:09:00.0\",\"codename\":\"TU104\",\"registers\":{\"broadcast\":{\"CONFIG0\":\"0x00000014\"}}}";DWORD n=0;const bool ok=WriteFile(f,text,sizeof(text)-1,&n,nullptr)!=0&&n==sizeof(text)-1;CloseHandle(f);return ok?0:3;}return 4;}
 wchar_t path[32768]{};const auto n=GetModuleFileNameW(nullptr,path,32768);check(n>0&&n<32768,"self helper path");run(path);std::printf("Timing capture client: %d failures\n",failures);return failures?1:0;}
