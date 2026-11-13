/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "i2c_session.h"
#include "profile_io.h"
#include "icx_preset.h"
#include <windows.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <vector>
using namespace druta::ui;
#define CHECK(x) do { if(!(x)) { std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x); std::abort(); } } while(false)
namespace {
dc_gpu_identity ident{}; std::map<uint32_t,std::vector<uint8_t>> regs; int writes=0, lock_depth=0; uint32_t clock_hold=0; bool frequency_available=true, mismatch_final=false, after_write=false, uncertain_selected=false; int loadline_reads=0;
void setup() {
    ident={}; ident.size=sizeof(ident); ident.nvapi_ok=ident.nvapi_selected=1;ident.nvapi_handle=99;ident.nvapi_devid=0x1234;ident.nvapi_subsys=0x8888;
    std::memcpy(ident.slot,"0000:09:00.0",sizeof("0000:09:00.0"));std::memcpy(ident.uuid,"GPU-test",sizeof("GPU-test"));
    regs={{0xb1,{5,0xfc,0x2d,0x13,0x77,0x42}},{0xb2,{4,0xfc,0,0,0}},{0x19,{9,0,0,0,0xaa,0xbb,0xcc,0xdd,0x04,0x01}},
          {0x1a,{2,4,0xaa}},{0x1b,{2,1,0}},{0x1c,{2,5,6}}};
    writes=lock_depth=0;clock_hold=0;frequency_available=true;mismatch_final=after_write=uncertain_selected=false;loadline_reads=0;
}
dc_status DC_CALL identity(const dc_gpu*,dc_gpu_identity* out) {*out=ident;return DC_OK;}
dc_status DC_CALL lock(dc_gpu*) {++lock_depth;return DC_OK;}
dc_status DC_CALL unlock(dc_gpu*) {--lock_depth;return DC_OK;}
dc_status DC_CALL read_scalar(dc_gpu*,uint32_t,uint32_t,uint32_t,uint32_t,uint32_t*,dc_error*) {return DC_E_UNAVAILABLE;}
dc_status DC_CALL read_block(dc_gpu*,uint32_t port,uint32_t addr,uint32_t reg,uint8_t* out,uint32_t size,dc_error*) {
    if(port!=1||addr!=0x2d||(reg==0x1c&&!frequency_available))return DC_E_UNAVAILABLE;
    if(mismatch_final && after_write && reg==0x1a && ++loadline_reads==2) regs[0x1a][1]=3;
    auto it=regs.find(reg);if(it==regs.end()||it->second.size()!=size)return DC_E_UNAVAILABLE;
    std::memcpy(out,it->second.data(),size);return DC_OK;
}
dc_status DC_CALL write_block(dc_gpu*,uint32_t family,uint32_t port,uint32_t addr,uint32_t reg,const uint8_t* value,uint32_t size,dc_error*) {
    CHECK(family==uint32_t(I2cFamily::icx) && port==1 && addr==0x2d && clock_hold && value); ++writes;after_write=true;
    if(reg==0xb2) {CHECK(size==5);regs[0xb2][1]=0xfc;}
    else if(reg==0x10) {CHECK(size==4);regs[0x19][1]=value[1];regs[0x19][2]=value[2];regs[0x19][3]=value[3];}
    else if(reg==0x18) {CHECK(size==4);regs[0x19][8]=value[2];regs[0x19][9]=value[3];}
    else {CHECK(size==2);uint32_t state=reg==0x11?0x1a:(reg==0x12||reg==0x1e?0x1b:0x1c);size_t index=reg==0x1e||reg==0x1f?2:1;regs[state][index]=value[1];}
    if(uncertain_selected && reg!=0xb2){uncertain_selected=false;return DC_E_FAILED;} return DC_OK;
}
dc_status DC_CALL architecture(dc_gpu*,uint32_t* value) {*value=7;return DC_OK;}
dc_status DC_CALL flags(dc_gpu*,int32_t* unrestricted,int32_t* xoc) {*unrestricted=0;*xoc=0;return DC_OK;}
dc_status DC_CALL clocks(dc_gpu*,dc_clock_row* rows,size_t cap,size_t* count) {*count=1;if(!rows||!cap)return DC_E_BUFFER;rows[0].mem_mhz=7000;rows[0].count=1;rows[0].graphics[0]=300;return DC_OK;}
dc_status DC_CALL clamp(dc_gpu*,int64_t low,int64_t high,dc_outcome* out) {CHECK(low==300&&high==300);clock_hold=300;out->ok=1;return DC_OK;}
dc_status DC_CALL release(dc_gpu*,int32_t,dc_outcome* out) {clock_hold=0;out->ok=1;return DC_OK;}
dc_status DC_CALL clock_status(dc_gpu*,dc_clk_lock_status* out) {*out={};out->locked=clock_hold!=0;out->min_mhz=out->max_mhz=clock_hold;return DC_OK;}
dc_status DC_CALL vf_status(dc_gpu*,int32_t,int64_t,dc_vf_lock_status* out) {*out={};return DC_OK;}
dc_status DC_CALL telemetry(dc_gpu*,dc_telemetry* out) {out->present=DC_T_CORE;out->core=clock_hold;return DC_OK;}
I2cApi api() {I2cApi a{};a.identity=identity;a.lock=lock;a.unlock=unlock;a.read=read_scalar;a.read_bytes=read_block;a.controller_write=write_block;a.architecture=architecture;a.flags=flags;a.clocks=clocks;a.clamp=clamp;a.release=release;a.clock_status=clock_status;a.vf_status=vf_status;a.telemetry=telemetry;a.sleep=[](unsigned){};return a;}
Command cmd(Command::Operation op,const I2cSession& s) {Command c;c.operation=op;c.i2c_selection_revision=s.data().selection_revision;return c;}
void select(I2cSession& s) {auto c=cmd(Command::Operation::i2c_select,s);c.i2c_family=I2cFamily::icx;c.i2c_route=I2cRoute{1,0x2d};CHECK(s.execute(c).ok);}
void gate(I2cSession& s) {auto c=cmd(Command::Operation::i2c_gate,s);c.enabled=true;CHECK(s.execute(c).ok);}
}
int main(){
 setup();I2cSession s(api(),reinterpret_cast<dc_gpu*>(1),ident);select(s);gate(s);
 auto document=capture_icx_preset(s.data().controller);document["settings"]["loadline_level"]=5;document["settings"]["fbvdd_request_mv"]=1310;
 IcxProfilePlan plan;const auto selection=s.data().selection_revision;const auto original=regs;
 CHECK(s.preflight_icx_profile(document,plan).ok && writes==0 && clock_hold==0 && !s.owned());
 CHECK(s.data().selection_revision==selection && !s.data().pending_preset && plan.prior["settings"]["loadline_level"]==4);
 CHECK(s.apply_icx_profile(plan).ok && writes>0 && s.owned() && regs[0x1a][1]==5 && regs[0x1a][2]==original.at(0x1a)[2]);
 const int applied_writes=writes;
 auto ordinary=cmd(Command::Operation::i2c_apply,s);ordinary.i2c_field="loadline";ordinary.value=6;
 CHECK(!s.execute(ordinary).ok && writes==applied_writes);
 auto wrong=plan;++wrong.selection_revision;CHECK(!s.restore_icx_profile(wrong).ok && writes==applied_writes);
 CHECK(s.restore_icx_profile(plan).ok && !s.owned() && clock_hold==0 && regs[0x1a]==original.at(0x1a));
 CHECK(capture_icx_preset(s.data().controller)["settings"]==plan.prior["settings"]);
 // Preflight and Apply are separated by a real current-state comparison.
 CHECK(s.preflight_icx_profile(document,plan).ok);regs[0x1a][1]=3;const int before_stale=writes;
 CHECK(!s.apply_icx_profile(plan).ok && writes==before_stale && !s.owned());regs[0x1a][1]=4;
 CHECK(s.preflight_icx_profile(document,plan).ok);std::strcpy(ident.uuid,"GPU-other");CHECK(!s.apply_icx_profile(plan).ok && writes==before_stale);
 setup();I2cSession foreign(api(),reinterpret_cast<dc_gpu*>(1),ident);select(foreign);gate(foreign);
 CHECK(foreign.preflight_icx_profile(document,plan).ok && foreign.apply_icx_profile(plan).ok);
 regs[0x1a][1]=6;const int foreign_writes=writes;CHECK(!foreign.restore_icx_profile(plan).ok && writes==foreign_writes && regs[0x1a][1]==6);
 setup();I2cSession partial(api(),reinterpret_cast<dc_gpu*>(1),ident);select(partial);gate(partial);frequency_available=false;
 IcxProfilePlan untouched;untouched.recipe="sentinel";CHECK(!partial.preflight_icx_profile(document,untouched).ok && untouched.recipe=="sentinel" && writes==0 && !partial.owned());
 setup();I2cSession release_first(api(),reinterpret_cast<dc_gpu*>(1),ident);select(release_first);gate(release_first);
 CHECK(release_first.preflight_icx_profile(document,plan).ok && release_first.apply_icx_profile(plan).ok);
 CHECK(release_first.release().ok);const int released=writes;CHECK(release_first.restore_icx_profile(plan).ok && writes==released);
 setup();I2cSession uncertain(api(),reinterpret_cast<dc_gpu*>(1),ident);select(uncertain);gate(uncertain);
 CHECK(uncertain.preflight_icx_profile(document,plan).ok);uncertain_selected=true;const auto attempted=uncertain.apply_icx_profile(plan);
 CHECK(!attempted.ok && attempted.may_have_changed && uncertain.owned());
 CHECK(uncertain.restore_icx_profile(plan).ok && !uncertain.owned() && clock_hold==0);
 CHECK(lock_depth==0);
 std::printf("iCX in-memory profile plans passed\n");return 0;
}