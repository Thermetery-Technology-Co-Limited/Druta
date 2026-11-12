/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "i2c_controls.h"
#include <cstdio>
#include <cstdlib>
#include <map>
#include <vector>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do { ++checks; if (!(x)) { std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x); std::abort(); } } while(false)
struct Fake : I2cTransport {
    bool connected=true; int transient=0, writes=0; bool fail_after_write=false, respond=true; uint8_t port=1, addr=0x20;
    std::map<int,uint32_t> regs{{0x00,0},{0xbe,0xa0},{0x21,120},{0xe1,0x4200},{0x01,0x80},{0x45,0},
        {0x23,0},{0x2a,32},{0x2d,0},{0x1f,48},{0x24,208},{0x8b,850},{0x8c,0xe020},{0x8d,400},{0x44,0},{0x78,0}};
    std::vector<uint8_t> written;
    bool bound() const override { return connected; }
    bool read(uint8_t p,uint8_t a,uint8_t reg,uint8_t,uint32_t& out) override {
        if (p!=port || a!=addr) return false;
        if (transient>0) { --transient; return false; }
        auto it=regs.find(reg); if(it==regs.end()) return false;
        out=(reg==0x8b && respond && (regs[0xe1]&0x10)) ? static_cast<uint32_t>(regs[0x21]*6.25) : it->second; return true;
    }
    bool write(uint8_t p,uint8_t a,uint8_t reg,uint8_t width,uint32_t value) override {
        CHECK(p==port && a==addr && width==2 && (reg==0x21 || reg==0xe1));
        if(reg==0xe1) CHECK(((regs[reg]^value)&~0x10u)==0);
        ++writes; written.push_back(reg); regs[reg]=value; return !fail_after_write;
    }
};
int main() {
    Fake t; I2cControls controls(t); auto candidates=controls.scan();
    CHECK(candidates.size()==1 && candidates[0].route.port==1 && candidates[0].route.addr7==0x20 && t.writes==0);
    CHECK(!controls.select({2,0x20}) && controls.select({1,0x20}));
    Fake alternate; alternate.port=5; alternate.addr=0x34; alternate.regs[0xbe]=0xb4;
    I2cControls other(alternate); auto other_candidates=other.scan();
    CHECK(other_candidates.size()==1 && other_candidates[0].route.port==5 && other_candidates[0].route.addr7==0x34);
    CHECK(other.select({5,0x34}) && other.status().readable && alternate.writes==0);
    auto state=controls.status(); CHECK(state.readable && state.capable && !state.writable && !state.enabled && state.controller_vout_mv==850);
    CHECK(!controls.apply(900,true,{}).ok && t.writes==0);
    controls.set_gate(true);
    I2cVerifyCallbacks cb; cb.core_clock_mhz=[] { return std::optional<double>(600); }; cb.sleep_ms=[](unsigned) {};
    auto verified=controls.verify(true,cb,{}); CHECK(verified.ok && !controls.has_owned_control());
    CHECK(t.regs[0x21]==120 && t.regs[0xe1]==0x4200);
    t.writes=0; t.written.clear();
    CHECK(controls.status().writable);
    t.transient=3; state=controls.status(); CHECK(!state.readable && state.stale && state.capable && !state.writable);
    CHECK(!controls.apply(900,false,{}).ok && t.writes==0);
    t.transient=2; state=controls.status(); CHECK(state.readable);
    t.regs.erase(0x8c); t.regs.erase(0x8d); CHECK(controls.status().readable && controls.status().capable);
    t.regs[0x8c]=0xe020; t.regs[0x8d]=400;
    Mp2888Control saved{}; int callback=0;
    auto result=controls.apply(900,true,[&](const auto& s){++callback; saved=s;});
    CHECK(result.ok && callback==1 && controls.has_owned_control());
    CHECK(t.writes==2 && t.written[0]==0x21 && t.written[1]==0xe1 && t.regs[0x21]==144 && t.regs[0xe1]==0x4210);
    CHECK(saved.command==120 && saved.mode==0x4200);
    auto update=controls.apply(925,true,{}); CHECK(update.ok && controls.has_owned_control() && t.regs[0x21]==148);
    t.regs[0x21]=149; CHECK(!controls.apply(931.25,true,{}).ok && controls.has_owned_control()); t.regs[0x21]=148;
    CHECK(!controls.select({1,0x20}) && !controls.restore(false).ok);
    t.regs[0xe1]^=0x20; CHECK(!controls.restore(true).ok && controls.has_owned_control()); t.regs[0xe1]^=0x20;
    CHECK(controls.restore(true).ok && !controls.has_owned_control());
    CHECK(t.writes==5 && t.written[3]==0xe1 && t.written[4]==0x21 && t.regs[0x21]==120 && t.regs[0xe1]==0x4200);
    CHECK(t.regs[0x23]==0 && t.regs[0x2a]==32 && t.regs[0x45]==0);
    t.regs[0x2a]=33; state=controls.status(); CHECK(state.readable && !state.capable && !state.writable);
    CHECK(!controls.apply(900,true,{}).ok && t.writes==5); t.regs[0x2a]=32;
    t.regs[0xe1]|=0x10; CHECK(!controls.apply(900,true,{}).ok && t.writes==5); t.regs[0xe1]&=~0x10u;
    t.regs[0x78]=0x40; CHECK(!controls.apply(900,true,{}).ok && t.writes==5); t.regs[0x78]=0;
    CHECK(!controls.apply(900.1,true,{}).ok && t.writes==5);
    t.fail_after_write=true; result=controls.apply(900,true,{});
    CHECK(!result.ok && result.may_have_changed && result.recovery_pending && controls.has_owned_control());
    CHECK(t.regs[0x21]==144 && t.regs[0xe1]==0x4200); t.fail_after_write=false;
    CHECK(controls.restore(true).ok && t.regs[0x21]==120 && t.regs[0xe1]==0x4200);
    Fake silent; silent.respond=false; I2cControls unverified(silent); CHECK(unverified.select({1,0x20}));
    unverified.set_gate(true); auto no_response=unverified.verify(true,cb,{});
    CHECK(!no_response.ok && !unverified.has_owned_control() && silent.regs[0x21]==120 && silent.regs[0xe1]==0x4200);
    CHECK(!unverified.apply(900,true,{}).ok);
    Fake unstable; I2cControls moving(unstable); CHECK(moving.select({1,0x20})); moving.set_gate(true);
    int samples=0; I2cVerifyCallbacks moving_cb; moving_cb.sleep_ms=[](unsigned) {};
    moving_cb.core_clock_mhz=[&] { return std::optional<double>(++samples>35 ? 601.0 : 600.0); };
    auto changed_clock=moving.verify(true,moving_cb,{});
    CHECK(!changed_clock.ok && !moving.has_owned_control() && unstable.writes>0);
    CHECK(unstable.regs[0x21]==120 && unstable.regs[0xe1]==0x4200);
    CHECK(!moving.apply(900,true,{}).ok);
    t.regs[0xbe]=0xa1; CHECK(!controls.select({1,0x20}));
    std::printf("i2c_controls_test: %u checks passed\n",checks);
}
