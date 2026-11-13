/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "i2c_session.h"
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <map>
#include <string>
#include <vector>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do { ++checks; if (!(x)) { std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x); std::abort(); } } while(false)
namespace {
dc_gpu_identity identity{};
std::map<int,uint32_t> regs;
std::vector<std::string> events;
int reads=0, writes=0, lock_depth=0, identity_reads=0, fail_reads=0; uint32_t held=0; bool foreign_vf=false, fail_clamp=false, fail_restore=false;
uint8_t wanted_port=3, wanted_addr=0x34; bool lose_clock_after_command=false, clock_unreadable=false;
bool duplicate_route=false, nct_mode=false; int typed_writes=0, typed_fail_reg=0; bool typed_uncertain=false;
void setup() {
    identity={}; identity.size=sizeof(identity); identity.nvapi_ok=identity.nvapi_selected=1;
    identity.nvapi_handle=1984; identity.nvapi_devid=0x12345678; identity.nvapi_subsys=0x87654321;
    std::memcpy(identity.slot,"0000:09:00.0",sizeof("0000:09:00.0"));
    std::memcpy(identity.uuid,"GPU-independent",sizeof("GPU-independent"));
    regs={{0x00,0},{0xbe,0xb4},{0x21,120},{0xe1,0x4200},{0x01,0x80},{0x45,0},{0x23,0},
          {0x2a,32},{0x2d,0},{0x1f,48},{0x24,208},{0x8b,850},{0x8c,0xe020},{0x8d,400},{0x44,0},{0x78,0}};
    events.clear(); reads=writes=lock_depth=identity_reads=fail_reads=0; held=0; foreign_vf=fail_clamp=fail_restore=false; lose_clock_after_command=clock_unreadable=false;
    duplicate_route=false; nct_mode=false; typed_writes=0; typed_fail_reg=0; typed_uncertain=false; wanted_port=3; wanted_addr=0x34;
}
dc_status DC_CALL get_identity(const dc_gpu*,dc_gpu_identity* out) { ++identity_reads; *out=identity; return DC_OK; }
dc_status DC_CALL gpu_lock(dc_gpu*) { ++lock_depth; return DC_OK; }
dc_status DC_CALL gpu_unlock(dc_gpu*) { --lock_depth; return DC_OK; }
dc_status DC_CALL bus_read(dc_gpu*,uint32_t port,uint32_t addr,uint32_t reg,uint32_t width,uint32_t* out,dc_error*) {
    ++reads; if(fail_reads>0) { --fail_reads; return DC_E_UNAVAILABLE; }
    if((port!=wanted_port && !(duplicate_route && port==4)) || addr!=wanted_addr || (width!=1 && width!=2)) return DC_E_UNAVAILABLE;
    auto it=regs.find(static_cast<int>(reg)); if(it==regs.end()) return DC_E_UNAVAILABLE;
    *out=(reg==0x8b && (regs[0xe1]&0x10)) ? static_cast<uint32_t>(regs[0x21]*6.25) : it->second; return DC_OK;
}
dc_status DC_CALL bus_write(dc_gpu*,uint32_t port,uint32_t addr,uint32_t reg,uint32_t width,uint32_t value,dc_error*) {
    CHECK(port==wanted_port && addr==wanted_addr && width==2 && (reg==0x21 || reg==0xe1));
    CHECK(held!=0); if (reg==0xe1) CHECK(((regs[0xe1]^value)&~0x10u)==0);
    events.push_back(reg==0xe1 ? "mode" : "command"); ++writes;
    if(fail_restore && reg==0xe1 && !(value&0x10)) return DC_E_FAILED;
    regs[static_cast<int>(reg)]=value;
    if (reg==0x21 && lose_clock_after_command) { clock_unreadable=true; lose_clock_after_command=false; }
    return DC_OK;
}
dc_status DC_CALL block_read(dc_gpu* gpu,uint32_t port,uint32_t addr,uint32_t reg,uint8_t* out,uint32_t count,dc_error* error) {
    if (!out || count<1 || count>4) return DC_E_UNAVAILABLE;
    uint32_t value=0; auto status=bus_read(gpu,port,addr,reg,count,&value,error);
    if(status==DC_OK) for(uint32_t i=0;i<count;++i) out[i]=static_cast<uint8_t>(value>>(8*i));
    return status;
}
dc_status DC_CALL typed_write(dc_gpu*,uint32_t family,uint32_t port,uint32_t addr,uint32_t reg,const uint8_t* data,uint32_t count,dc_error*) {
    CHECK(nct_mode && family==static_cast<uint32_t>(I2cFamily::nct3933) && port==wanted_port && addr==wanted_addr);
    CHECK(reg>=1 && reg<=3 && data && count==1); ++typed_writes;
    events.push_back("out"+std::to_string(reg));
    if (static_cast<int>(reg)==typed_fail_reg) {
        typed_fail_reg=0; if (typed_uncertain) { regs[static_cast<int>(reg)]=data[0]; fail_reads=4; }
        return DC_E_FAILED;
    }
    regs[static_cast<int>(reg)]=data[0]; return DC_OK;
}
dc_status DC_CALL icx_arch(dc_gpu*,uint32_t* out) {*out=7;return DC_OK;}
dc_status DC_CALL icx_read(dc_gpu*,uint32_t port,uint32_t addr,uint32_t reg,uint8_t* out,uint32_t count,dc_error*) {
    if(port!=1 || addr!=0x2d || !out || count==0) return DC_E_UNAVAILABLE;
    std::fill(out,out+count,0); out[0]=static_cast<uint8_t>(count-1);
    if(reg==0xb1 && count==6) {out[1]=0xfc;out[2]=0x2d;out[3]=0x13;return DC_OK;}
    if(reg==0x19 && count==10) {out[1]=1;out[2]=180;out[8]=4;out[9]=1;return DC_OK;}
    if((reg==0x1a || reg==0x1b || reg==0x1c) && count==3) {if(reg==0x1c){out[1]=6;out[2]=6;}return DC_OK;}
    return DC_E_UNAVAILABLE;
}
dc_status DC_CALL clock_table(dc_gpu*,dc_clock_row* rows,size_t capacity,size_t* count) {
    *count=2; if (!rows || capacity<2) return DC_E_BUFFER;
    rows[0].mem_mhz=1000; rows[0].count=1; rows[0].graphics[0]=210;
    rows[1].mem_mhz=7000; rows[1].count=2; rows[1].graphics[0]=999; rows[1].graphics[1]=333; return DC_OK;
}
dc_status DC_CALL clamp(dc_gpu*,int64_t low,int64_t high,dc_outcome* out) {
    CHECK(low==333 && high==333); events.push_back("clamp"); held=static_cast<uint32_t>(low); out->ok=fail_clamp?0:1; return DC_OK;
}
dc_status DC_CALL release(dc_gpu*,int32_t,dc_outcome* out) {
    events.push_back("release"); held=0; out->ok=1; return DC_OK;
}
dc_status DC_CALL clock_status(dc_gpu*,dc_clk_lock_status* out) { if (clock_unreadable) return DC_E_UNAVAILABLE; out->locked=held!=0; out->min_mhz=held; out->max_mhz=held; return DC_OK; }
dc_status DC_CALL vf_status(dc_gpu*,int32_t,int64_t,dc_vf_lock_status* out) { out->locked=foreign_vf; return DC_OK; }
dc_status DC_CALL telemetry(dc_gpu*,dc_telemetry* out) { out->present=DC_T_CORE; out->core=held; return DC_OK; }
I2cApi api() {
    I2cApi a{}; a.identity=get_identity; a.read=bus_read; a.write=bus_write; a.lock=gpu_lock; a.unlock=gpu_unlock;
    a.clocks=clock_table; a.clamp=clamp; a.release=release; a.clock_status=clock_status; a.vf_status=vf_status; a.telemetry=telemetry;
    a.sleep=[](unsigned) {}; return a;
}
Command command(Command::Operation op) { Command c; c.operation=op; return c; }
void select_gate(I2cSession& s) {
    auto scan_command=command(Command::Operation::i2c_scan); scan_command.i2c_family=I2cFamily::mp2888;
    auto scan=s.execute(scan_command); CHECK(scan.ok && s.data().candidates.size()==1);
    auto select=command(Command::Operation::i2c_select); select.i2c_route=I2cRoute{wanted_port,wanted_addr}; select.i2c_family=I2cFamily::mp2888; select.i2c_recipe="mp2888a-pmbus"; CHECK(s.execute(select).ok);
    auto gate=command(Command::Operation::i2c_gate); gate.enabled=true; const int before_gate_reads=reads;
    CHECK(s.execute(gate).ok && reads==before_gate_reads);
}
}
#include <stdexcept>
namespace {
void history_nct() {
 setup();nct_mode=true;wanted_port=1;wanted_addr=0x15;
 regs={{0x5d,0x39},{0x5e,0x33},{5,0},{1,0},{2,0},{3,0}};
 auto a=api();a.read_bytes=block_read;a.controller_write=typed_write;
 I2cSession s(a,reinterpret_cast<dc_gpu*>(1),identity);
 unsigned captures=0;s.set_history_callback([&]{++captures;events.push_back("history");CHECK(!s.capture_full_profile().i2c.is_null());});
 auto select=command(Command::Operation::i2c_select);select.i2c_route=I2cRoute{1,0x15};select.i2c_family=I2cFamily::nct3933;
 CHECK(s.execute(select).ok);auto gate=command(Command::Operation::i2c_gate);gate.enabled=true;CHECK(s.execute(gate).ok);
 CHECK(s.execute(command(Command::Operation::i2c_verify)).ok);CHECK(s.execute(command(Command::Operation::i2c_read)).ok&&captures==0);
 auto apply=command(Command::Operation::i2c_apply);apply.i2c_field="out1";apply.value=0;
 CHECK(s.execute(apply).ok&&captures==0&&typed_writes==0);apply.value=21;CHECK(!s.execute(apply).ok&&captures==0);
 apply.value=20;events.clear();CHECK(s.execute(apply).ok&&captures==1&&events==std::vector<std::string>({"history","out1"}));
 CHECK(s.execute(apply).ok&&captures==1); // Existing request no-op must not create disk history.
 events.clear();auto reset=command(Command::Operation::i2c_reset);reset.i2c_field="out1";
 CHECK(s.execute(reset).ok&&captures==2&&events==std::vector<std::string>({"history","out1"}));
 CHECK(s.release().ok&&captures==2); // Lifecycle cleanup/restoration is not a new user edit.
 regs[1]=0x82;regs[2]=3;regs[3]=0x84;events.clear();
 auto zero=command(Command::Operation::i2c_zero_nct_outputs);CHECK(s.execute(zero).ok&&captures==3);
 CHECK(events==std::vector<std::string>({"history","out1","out2","out3"}));
 CHECK(s.execute(zero).ok&&captures==3);CHECK(s.release().ok&&captures==3);
 events.clear();typed_fail_reg=2;typed_uncertain=true;CHECK(!s.execute(zero).ok&&captures==4);
 fail_reads=0;typed_uncertain=false;CHECK(s.release().ok&&captures==4);
 auto stale=apply;stale.i2c_selection_revision=s.data().selection_revision+1;CHECK(!s.execute(stale).ok&&captures==4);
 s.set_history_callback([&]{++captures;throw std::runtime_error("History unavailable");});
 apply.value=30;CHECK(s.execute(apply).ok&&captures==5&&regs[1]==0x83);CHECK(s.release().ok&&captures==5);
 auto document=s.capture_full_profile().i2c;document["current_dac_control"]["outputs"]=ProfileJson::array({128,5,0});
 I2cProfilePlan plan;CHECK(s.preflight_i2c_profile(document,plan).ok);const auto prior_writes=typed_writes;
 CHECK(!s.apply_i2c_profile(plan,{}).ok&&typed_writes==prior_writes&&!s.owned()&&captures==6);
 s.set_history_callback([&]{++captures;events.push_back("history");});events.clear();I2cProfileReceipt receipt;
 CHECK(s.apply_i2c_profile(plan,[&](const auto& r){receipt=r;events.push_back("journal");}).ok&&captures==7);
 CHECK(events.size()>=3&&events[0]=="history"&&events[1]=="journal");
 CHECK(s.restore_i2c_profile(receipt).ok&&captures==7);
 document["current_dac_control"]["outputs"]=ProfileJson::array({regs[1],regs[2],regs[3]});
 CHECK(s.preflight_i2c_profile(document,plan).ok&&s.apply_i2c_profile(plan,{}).ok&&captures==7&&!s.owned());

}
void history_icx_noop() {
 setup();auto a=api();a.read_bytes=icx_read;a.architecture=icx_arch;a.controller_write=typed_write;
 I2cSession s(a,reinterpret_cast<dc_gpu*>(1),identity);unsigned captures=0;s.set_history_callback([&]{++captures;});
 auto select=command(Command::Operation::i2c_select);select.i2c_family=I2cFamily::icx;select.i2c_route=I2cRoute{1,0x2d};CHECK(s.execute(select).ok);
 auto gate=command(Command::Operation::i2c_gate);gate.enabled=true;CHECK(s.execute(gate).ok);CHECK(s.execute(command(Command::Operation::i2c_verify)).ok);
 auto apply=command(Command::Operation::i2c_apply);apply.i2c_field="nvvdd";apply.value=900;
 CHECK(s.execute(apply).ok&&captures==0&&held==0&&!s.owned());
 apply.value=903;CHECK(!s.execute(apply).ok&&captures==0&&held==0);
}
void history_pmbus() {
 setup();I2cSession s(api(),reinterpret_cast<dc_gpu*>(1),identity);unsigned captures=0;
 s.set_history_callback([&]{++captures;events.push_back("history");});select_gate(s);
 CHECK(s.execute(command(Command::Operation::i2c_verify)).ok&&captures==0);
 auto apply=command(Command::Operation::i2c_apply);apply.value=901;CHECK(!s.execute(apply).ok&&captures==0);
 apply.value=900;events.clear();CHECK(s.execute(apply).ok&&captures==1&&!events.empty()&&events.front()=="history");
 CHECK(events.size()>1&&events[1]=="clamp");
 events.clear();CHECK(s.execute(apply).ok&&captures==1&&events.empty());
 auto automatic=command(Command::Operation::i2c_reset);events.clear();CHECK(s.execute(automatic).ok&&captures==2&&events.front()=="history"&&!s.owned());
 CHECK(s.release().ok&&captures==2);
}
}
int main(){history_nct();history_icx_noop();history_pmbus();std::printf("I2C history: %u checks\n",checks);return 0;}
