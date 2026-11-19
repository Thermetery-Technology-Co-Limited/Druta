// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "timings_service.h"
#include <cstdio>
#include <cstring>
using namespace druta::ui;
namespace {
int failures=0;void check(bool b,const char* s){if(!b){++failures;std::printf("FAIL: %s\n",s);}}
dc_gpu_identity id{};
dc_status DC_CALL identity(const dc_gpu*,dc_gpu_identity* out){*out=id;return DC_OK;}
dc_status DC_CALL lock(dc_gpu*){return DC_OK;}dc_status DC_CALL unlock(dc_gpu*){return DC_OK;}
dc_status DC_CALL telemetry(dc_gpu*,dc_telemetry* out){out->present=DC_T_MEM|DC_T_PSTATE|DC_T_MEM_OFF;out->mem=7000;out->pstate=0;out->mem_off=0;return DC_OK;}
dc_status DC_CALL caps(dc_gpu*,dc_capabilities* out){out->mem_clock_count=1;out->mem_clocks[0]=7000;out->present=DC_C_MEM_DIV;out->mem_div=4;return DC_OK;}
struct Mock:TimingTransport {
 bool pinned=false,changed=false,change_during_save=false;int pins=0,unpins=0,runs=0;
 bool pin(std::string&)override{++pins;pinned=true;return true;}bool unchanged(std::string&)override{return pinned&&!changed;}
 std::string token()const override{return pinned?"helper-token":"";}void unpin()override{++unpins;pinned=false;}
 TimingProcessResult run(TimingCommand op,const std::string&,const std::string&,uint32_t,bool,bool)override{
  ++runs;TimingProcessResult r;r.launched=true;
  if(op==TimingCommand::fields)r.output="RC CONFIG0 [7:0] 255 Cycles\n";
  else if(op==TimingCommand::dump)r.output="0000:09:00.0 TU104 (Turing)\n [broadcast] base 0x001000\n CONFIG0 @0x001000 = 0x00000014 (DOCUMENTED)\n";
  else if(op==TimingCommand::save){r.output="{\"slot\":\"0000:09:00.0\",\"codename\":\"TU104\",\"registers\":{\"broadcast\":{\"CONFIG0\":\"0x00000014\"},\"fbpa0\":{\"CONFIG0\":\"0x00000015\"}}}";if(change_during_save)changed=true;}
  else {r.exit_code=1;}return r;
 }
};
}
int main(){id.size=sizeof(id);id.nvapi_ok=id.nvapi_selected=1;id.nvapi_handle=42;std::strcpy(id.slot,"0000:09:00.0");std::strcpy(id.uuid,"GPU-other");
 TimingsApi api;api.identity=identity;api.telemetry=telemetry;api.capabilities=caps;api.lock=lock;api.unlock=unlock;Mock m;TimingsService service(api,reinterpret_cast<dc_gpu*>(1),id,m);
 auto absent=service.capture_pinned();check(!absent.ok&&m.pins==0&&m.runs==0,"unowned pin refuses without helper calls");
 std::string why;check(m.pin(why),"outer helper pin");auto captured=service.capture_pinned();
 check(captured.ok&&captured.helper_token=="helper-token"&&m.pins==1&&m.unpins==0&&m.pinned,"outer pin survives capture with exact token");
 check(captured.diagnostic_evidence.partitions.size()==1 && captured.diagnostic_evidence.memory_clock_divisor==4 && service.initial_snapshot(),"diagnostics and initial state retained");
 m.changed=true;const int prior=m.runs;auto changed=service.capture_pinned();check(!changed.ok&&m.runs==prior&&m.pinned,"changed helper refused without repin");
 m.changed=false;m.change_during_save=true;auto during=service.capture_pinned();check(!during.ok&&during.helper_token.empty()&&m.pinned,"changed helper during capture refused");
 m.changed=false;m.change_during_save=false;m.unpin();auto ordinary=service.capture();check(ordinary.ok&&ordinary.helper_token=="helper-token"&&!m.pinned&&m.pins==2&&m.unpins==2,"ordinary capture owns and releases its pin");
 std::printf("Timing capture service: %d failures\n",failures);return failures?1:0;}
