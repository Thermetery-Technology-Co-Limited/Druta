/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "i2c_session.h"
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
int main() {
    setup(); I2cSession s(api(),reinterpret_cast<dc_gpu*>(1),identity);
    CHECK(s.data().available && reads==0 && writes==0 && !s.owned());
    CHECK(s.data().control_profiles.size()==2);
    CHECK(s.data().control_profiles[1].available && s.data().control_profiles[1].presets_mv.empty());
    select_gate(s); CHECK(writes==0);
    const auto selected_revision=s.data().selection_revision; const int before_reconnect=reads;
    fail_reads=100; auto reconnect=command(Command::Operation::i2c_scan); reconnect.i2c_family=I2cFamily::mp2888;
    CHECK(!s.execute(reconnect).ok);
    CHECK(s.data().selected && s.data().selected->port==wanted_port && s.data().selected->addr7==wanted_addr);
    CHECK(s.data().control_profile=="mp2888a-pmbus" && s.data().selection_revision==selected_revision && reads-before_reconnect<100 && writes==0);
    fail_reads=0; CHECK(s.execute(command(Command::Operation::i2c_read)).ok);
    CHECK(s.data().selected && s.data().selected->port==wanted_port && s.data().selected->addr7==wanted_addr);
    const auto& profile=s.data().control_profiles[1];
    CHECK(profile.step_mv==6.25 && !profile.presets_mv.empty());
    for(double value:profile.presets_mv) CHECK(value>=*s.data().status.min_mv && value<=*s.data().status.max_mv && std::floor(value/profile.step_mv)==value/profile.step_mv);
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
    CHECK(!readonly.data().control_profiles[1].available);
    CHECK(lock_depth==0);
    setup(); I2cSession discovered(api(),reinterpret_cast<dc_gpu*>(1),identity);
    auto mp_scan=command(Command::Operation::i2c_scan); mp_scan.i2c_family=I2cFamily::mp2888;
    CHECK(discovered.execute(mp_scan).ok);
    CHECK(discovered.data().selected && !discovered.data().gate && !discovered.data().verified && writes==0 && held==0);
    auto manual_recipe=command(Command::Operation::i2c_recipe); manual_recipe.i2c_recipe="mp2888a-pmbus";
    CHECK(discovered.execute(manual_recipe).ok);
    auto one_click=command(Command::Operation::i2c_verify); one_click.enabled=true;
    one_click.i2c_route=I2cRoute{wanted_port,wanted_addr};
    CHECK(!discovered.execute(one_click).ok && !discovered.data().gate && writes==0);
    auto explicit_gate=command(Command::Operation::i2c_gate);explicit_gate.enabled=true;
    CHECK(discovered.execute(explicit_gate).ok);
    CHECK(discovered.execute(one_click).ok && discovered.data().gate && discovered.data().verified && !discovered.owned() && held==0);
    CHECK(regs[0x21]==120 && regs[0xe1]==0x4200);
    setup(); duplicate_route=true; I2cSession ambiguous(api(),reinterpret_cast<dc_gpu*>(1),identity);
    CHECK(ambiguous.execute(mp_scan).ok);
    CHECK(ambiguous.data().candidates.size()==2 && !ambiguous.data().selected && writes==0 && held==0);
    setup(); nct_mode=true; wanted_port=1; wanted_addr=0x15;
    regs={{0x5d,0x39},{0x5e,0x33},{5,0},{1,0},{2,0},{3,0}};
    auto typed_api=api(); typed_api.read_bytes=block_read; typed_api.controller_write=typed_write;
    I2cSession typed(typed_api,reinterpret_cast<dc_gpu*>(1),identity);
    auto early_gate=command(Command::Operation::i2c_gate); early_gate.enabled=true;
    CHECK(typed.execute(early_gate).ok && reads==0 && writes==0 && typed_writes==0 && identity_reads==0 && lock_depth==0);
    auto nct_select=command(Command::Operation::i2c_select); nct_select.i2c_route=I2cRoute{1,0x15}; nct_select.i2c_family=I2cFamily::nct3933;
    CHECK(typed.execute(nct_select).ok && typed.data().family==I2cFamily::nct3933 && typed.data().gate);
    auto nct_apply=command(Command::Operation::i2c_apply); nct_apply.i2c_field="out1"; nct_apply.value=20;
    nct_apply.i2c_selection_revision=typed.data().selection_revision;
    CHECK(typed.execute(nct_apply).ok && typed_writes==1 && regs[1]==0x82 && typed.owned());
    CHECK(typed.execute(command(Command::Operation::i2c_release)).ok && typed_writes==2 && regs[1]==0 && !typed.owned());
    auto stale=nct_apply; CHECK(typed.execute(nct_select).ok && typed.data().selection_revision!=stale.i2c_selection_revision);
    const int stale_reads=reads, stale_writes=typed_writes;
    CHECK(!typed.execute(stale).ok && reads==stale_reads && typed_writes==stale_writes && !typed.owned());
    nct_apply.i2c_selection_revision=typed.data().selection_revision;
    CHECK(typed.execute(nct_apply).ok && typed.owned());
    auto stale_gate=command(Command::Operation::i2c_gate); stale_gate.enabled=false; stale_gate.i2c_selection_revision=stale.i2c_selection_revision;
    const int before_stale_gate_reads=reads, before_stale_gate_writes=typed_writes;
    CHECK(!typed.execute(stale_gate).ok && typed.data().gate && typed.owned());
    CHECK(reads==before_stale_gate_reads && typed_writes==before_stale_gate_writes);
    CHECK(typed.execute(command(Command::Operation::i2c_release)).ok && !typed.owned());
    auto zero=command(Command::Operation::i2c_zero_nct_outputs);
    zero.i2c_route=I2cRoute{1,0x15}; zero.i2c_selection_revision=typed.data().selection_revision;
    regs[1]=0x82; regs[2]=3; regs[3]=0x84; events.clear();
    auto zeroed=typed.execute(zero);
    CHECK(zeroed.ok && zeroed.may_have_changed && regs[1]==0 && regs[2]==0 && regs[3]==0 && typed.owned());
    CHECK(events==std::vector<std::string>({"out1","out2","out3"}));
    CHECK(zeroed.message.find("out1, out2, out3")!=std::string::npos);
    CHECK(typed.execute(command(Command::Operation::i2c_release)).ok && !typed.owned());
    CHECK(regs[1]==0x82 && regs[2]==3 && regs[3]==0x84);
    typed_fail_reg=2; events.clear(); auto partial_zero=typed.execute(zero);
    CHECK(!partial_zero.ok && partial_zero.may_have_changed && typed.owned());
    CHECK(partial_zero.message.find("out2")!=std::string::npos && partial_zero.message.find("completed: out1")!=std::string::npos);
    CHECK(regs[1]==0 && regs[2]==3 && regs[3]==0x84 && events==std::vector<std::string>({"out1","out2"}));
    CHECK(typed.execute(command(Command::Operation::i2c_release)).ok && regs[1]==0x82 && !typed.owned());
    typed_fail_reg=2; typed_uncertain=true; events.clear(); auto uncertain_zero=typed.execute(zero);
    CHECK(!uncertain_zero.ok && uncertain_zero.may_have_changed && typed.owned());
    CHECK(uncertain_zero.message.find("restoration pending")!=std::string::npos && regs[3]==0x84);
    CHECK(events==std::vector<std::string>({"out1","out2"}));
    fail_reads=0; typed_uncertain=false; CHECK(typed.execute(command(Command::Operation::i2c_release)).ok);
    CHECK(regs[1]==0x82 && regs[2]==3 && regs[3]==0x84 && !typed.owned());
    auto stale_zero=zero; --stale_zero.i2c_selection_revision;
    const int before_zero_refusal=typed_writes; CHECK(!typed.execute(stale_zero).ok && typed_writes==before_zero_refusal);
    auto wrong_zero=zero; wrong_zero.i2c_route=I2cRoute{1,0x14};
    CHECK(!typed.execute(wrong_zero).ok && typed_writes==before_zero_refusal);
    auto gate_off=command(Command::Operation::i2c_gate); gate_off.enabled=false; CHECK(typed.execute(gate_off).ok);
    CHECK(!typed.execute(zero).ok && typed_writes==before_zero_refusal);
    CHECK(I2cControls::nominal_presets(900.1,906.24).empty());
    CHECK(I2cControls::nominal_presets(-1,1000).empty());
    CHECK(I2cControls::nominal_presets(0,4000).empty());
    const auto narrow=I2cControls::nominal_presets(901,913);
    CHECK(narrow.size()==2 && narrow[0]==906.25 && narrow[1]==912.5);
    CHECK(lock_depth==0); std::printf("i2c_session_test: %u checks passed\n",checks);
}
