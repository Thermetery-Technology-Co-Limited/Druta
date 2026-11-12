/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "i2c_session.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
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
int reads=0, writes=0, lock_depth=0; uint32_t held=0; bool foreign_vf=false, fail_clamp=false, fail_restore=false;
uint8_t wanted_port=3, wanted_addr=0x34; bool lose_clock_after_command=false, clock_unreadable=false;
void setup() {
    identity={}; identity.size=sizeof(identity); identity.nvapi_ok=identity.nvapi_selected=1;
    identity.nvapi_handle=1984; identity.nvapi_devid=0x12345678; identity.nvapi_subsys=0x87654321;
    std::memcpy(identity.slot,"0000:09:00.0",sizeof("0000:09:00.0"));
    std::memcpy(identity.uuid,"GPU-independent",sizeof("GPU-independent"));
    regs={{0x00,0},{0xbe,0xb4},{0x21,120},{0xe1,0x4200},{0x01,0x80},{0x45,0},{0x23,0},
          {0x2a,32},{0x2d,0},{0x1f,48},{0x24,208},{0x8b,850},{0x8c,0xe020},{0x8d,400},{0x44,0},{0x78,0}};
    events.clear(); reads=writes=lock_depth=0; held=0; foreign_vf=fail_clamp=fail_restore=false; lose_clock_after_command=clock_unreadable=false;
}
dc_status DC_CALL get_identity(const dc_gpu*,dc_gpu_identity* out) { *out=identity; return DC_OK; }
dc_status DC_CALL gpu_lock(dc_gpu*) { ++lock_depth; return DC_OK; }
dc_status DC_CALL gpu_unlock(dc_gpu*) { --lock_depth; return DC_OK; }
dc_status DC_CALL bus_read(dc_gpu*,uint32_t port,uint32_t addr,uint32_t reg,uint32_t width,uint32_t* out,dc_error*) {
    ++reads; if(port!=wanted_port || addr!=wanted_addr || (width!=1 && width!=2)) return DC_E_UNAVAILABLE;
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
    auto scan=s.execute(command(Command::Operation::i2c_scan)); CHECK(scan.ok && s.data().candidates.size()==1);
    auto select=command(Command::Operation::i2c_select); select.i2c_route=I2cRoute{wanted_port,wanted_addr}; CHECK(s.execute(select).ok);
    auto gate=command(Command::Operation::i2c_gate); gate.enabled=true; const int before_gate_reads=reads;
    CHECK(s.execute(gate).ok && reads==before_gate_reads);
}
}
int main() {
    setup(); I2cSession s(api(),reinterpret_cast<dc_gpu*>(1),identity);
    CHECK(s.data().available && reads==0 && writes==0 && !s.owned());
    select_gate(s); CHECK(writes==0);
    foreign_vf=true; auto refused=s.execute(command(Command::Operation::i2c_verify));
    CHECK(!refused.ok && writes==0 && held==0); foreign_vf=false;
    auto verified=s.execute(command(Command::Operation::i2c_verify));
    CHECK(verified.ok && s.data().verified && !s.owned() && held==0);
    CHECK(regs[0x21]==120 && regs[0xe1]==0x4200 && events.back()=="release");
    CHECK(s.data().status.request_eligible && s.data().status.writable);
    regs.erase(0x00); CHECK(s.execute(command(Command::Operation::i2c_read)).ok);
    CHECK(s.data().status.stale && !s.data().status.request_eligible && !s.data().status.writable);
    auto gate=command(Command::Operation::i2c_gate); gate.enabled=false; const int after_failed_read=reads;
    CHECK(s.execute(gate).ok); gate.enabled=true; CHECK(s.execute(gate).ok);
    CHECK(reads==after_failed_read && !s.data().status.writable);
    regs[0x00]=0; CHECK(s.execute(command(Command::Operation::i2c_read)).ok && s.data().status.request_eligible);
    regs[0xe1]|=0x10; CHECK(s.execute(command(Command::Operation::i2c_read)).ok);
    CHECK(s.data().status.enabled && !s.data().status.request_eligible && !s.data().status.writable);
    gate.enabled=false; const int foreign_reads=reads; CHECK(s.execute(gate).ok);
    gate.enabled=true; CHECK(s.execute(gate).ok && reads==foreign_reads && !s.data().status.writable);
    regs[0xe1]&=~0x10u; CHECK(s.execute(command(Command::Operation::i2c_read)).ok && s.data().status.writable);    auto apply=command(Command::Operation::i2c_apply); apply.value=900;
    const int before_clock_race=writes; lose_clock_after_command=true;
    auto raced=s.execute(apply);
    CHECK(!raced.ok && s.owned() && held==333 && writes==before_clock_race+1);
    CHECK(regs[0x21]==144 && regs[0xe1]==0x4200 && events.back()=="command");
    clock_unreadable=false; CHECK(s.execute(command(Command::Operation::i2c_release)).ok && !s.owned());
    CHECK(regs[0x21]==120 && regs[0xe1]==0x4200 && held==0);
    apply.value=2000;
    const int before_refusal_writes=writes; auto invalid=s.execute(apply);
    CHECK(!invalid.ok && writes==before_refusal_writes && !s.owned() && held==0);
    apply.value=900;
    CHECK(s.execute(apply).ok && s.owned() && held==333 && regs[0x21]==144 && (regs[0xe1]&0x10));
    apply.value=925; CHECK(s.execute(apply).ok && regs[0x21]==148);
    const auto before_release=events.size(); CHECK(s.execute(command(Command::Operation::i2c_release)).ok);
    CHECK(!s.owned() && held==0 && regs[0x21]==120 && regs[0xe1]==0x4200);
    CHECK(events.size()>=before_release+3 && events[events.size()-3]=="mode" && events[events.size()-2]=="command" && events.back()=="release");
    CHECK(s.execute(apply).ok && held==333); fail_restore=true; auto failed=s.execute(command(Command::Operation::i2c_release));
    CHECK(!failed.ok && s.owned() && held==333 && events.back()=="mode");
    fail_restore=false; CHECK(s.execute(command(Command::Operation::i2c_release)).ok && !s.owned());
    fail_clamp=true; auto failed_clamp=s.execute(apply); CHECK(!failed_clamp.ok && s.owned() && held==333);
    fail_clamp=false; CHECK(s.execute(command(Command::Operation::i2c_release)).ok && !s.owned());
    CHECK(s.execute(apply).ok && s.owned()); identity.nvapi_handle++;
    CHECK(!s.execute(command(Command::Operation::i2c_release)).ok && s.owned() && held==333);
    identity.nvapi_handle--; CHECK(s.execute(command(Command::Operation::i2c_release)).ok && held==0);
    CHECK(lock_depth==0);
    setup(); auto read_only_api=api(); read_only_api.write=nullptr;
    I2cSession readonly(read_only_api,reinterpret_cast<dc_gpu*>(1),identity);
    CHECK(readonly.data().available && reads==0); select_gate(readonly);
    CHECK(readonly.execute(command(Command::Operation::i2c_read)).ok && readonly.data().status.readable);
    CHECK(!readonly.execute(command(Command::Operation::i2c_verify)).ok && writes==0 && held==0);
    CHECK(lock_depth==0);    std::printf("i2c_session_test: %u checks passed\n",checks);
}
