/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "i2c_driver.h"
#include <algorithm>
#include <cmath>
#include <vector>

namespace druta::ui {
namespace {
class MpOffset final : public ControllerDriver {
    ControllerIo& io; I2cRoute route;
    bool identified=false, verified=false, recovery=false;
    std::optional<unsigned> initial, last, attempted;
    struct State { unsigned address=0, word=0, limit=0, mode=0, voltage=0; };
    static int sign(unsigned x) {x &= 255; return x&128 ? int(x)-256 : int(x);}
    bool get(uint8_t reg,size_t size,unsigned& out) {
        for(int i=0;i<3;++i) {std::vector<uint8_t> bytes; if(io.controller_bound() && io.read_bytes(route,reg,size,bytes) && bytes.size()==size && io.controller_bound()) {out=0; for(size_t j=0;j<size;++j) out|=unsigned(bytes[j])<<(8*j); return true;}}
        return false;
    }
    bool fingerprint() {
        if(route.port>=8 || route.addr7<8 || route.addr7>0x77) return false;
        struct Sample {unsigned addr,word,limit,vout,current,temp,config;};
        auto one=[&](Sample& s) {return get(0xbe,1,s.addr) && (s.addr&127)==route.addr7 && get(0x23,2,s.word) && !(s.word&0xff00) && sign(s.word)>=-111 && sign(s.word)<=112 && get(0x24,2,s.limit) && !(s.limit&0xfe00) && s.limit*6.25>=300 && get(0x8b,2,s.vout) && !(s.vout&0xf000) && s.vout>=300 && s.vout<=2000 && get(0x8c,2,s.current) && (s.current&0xf000)==0xe000 && get(0x8d,2,s.temp) && !(s.temp&0xf800) && s.temp<=1500 && get(0x44,2,s.config);};
        Sample a{},b{}; return one(a)&&one(b)&&a.addr==b.addr&&a.word==b.word&&a.limit==b.limit&&((a.config^b.config)&8)==0&&std::abs(int(a.vout)-int(b.vout))<=500&&std::abs(int(a.temp)-int(b.temp))<=100;
    }
    bool capture(State& s, bool need_voltage=true) {
        unsigned addr2,word2,mode2,limit2;
        if(!identified || !get(0xbe,1,s.address) || (s.address&127)!=route.addr7 || !get(0x23,2,s.word) || (s.word&0xff00) || sign(s.word)<-111 || sign(s.word)>112 || !get(0x24,2,s.limit) || (s.limit&0xfe00) || s.limit*6.25<300 || !get(0xe1,2,s.mode) || (s.mode&0x10) || (need_voltage && (!get(0x8b,2,s.voltage) || (s.voltage&0xf000) || s.voltage<300 || s.voltage>2000)) || !get(0xbe,1,addr2) || !get(0x23,2,word2) || !get(0xe1,2,mode2) || !get(0x24,2,limit2)) return false;
        return s.address==addr2 && s.word==word2 && s.mode==mode2 && s.limit==limit2;
    }
    bool same(const State& a,const State& b) {return a.address==b.address && a.word==b.word && a.limit==b.limit && a.mode==b.mode;}
    bool write(unsigned word) {return io.write_bytes(I2cFamily::mp2888,route,0x23,{uint8_t(word),uint8_t(word>>8)});}
    bool restore_word(unsigned word) {
        State before,after; if(!capture(before,false)) return false;
        // Only accept a value known to have belonged to this transaction.
        if(before.word!=word && (!last || before.word!=*last) && (!attempted || before.word!=*attempted)) return false;
        if(before.word!=word && !write(word)) return false;
        return capture(after,false) && after.word==word && before.address==after.address && before.limit==after.limit && before.mode==after.mode;
    }
    I2cResult set(double mv,bool xoc) {
        if(!std::isfinite(mv) || (!xoc && (mv< -200 || mv>100))) return {false,false,recovery,"Offset exceeds the selected mode envelope"};
        double steps=std::nearbyint(mv/6.25); if(steps< -111 || steps>112) return {false,false,recovery,"Offset is outside the controller encoding"};
        State before,check; if(!capture(before)) return {false,false,recovery,"Controller state or voltage unavailable; foreign manual mode is refused"};
        if(last && before.word!=*last) return {false,false,true,"Owned offset changed externally"};
        double predicted=double(before.voltage)-sign(before.word)*6.25+steps*6.25;
        double ceiling=std::min(before.limit*6.25,xoc?2000.0:1200.0);
        if((!xoc && (before.voltage<400 || before.voltage>1300)) || predicted>ceiling || predicted>2000) return {false,false,recovery,"Controller VOUT or projected estimate exceeds live limits"};
        unsigned target=(before.word&0xff00) | (unsigned(int(steps))&255);
        if(target==before.word) return {true,false,recovery,"Offset already matches"};
        if(!capture(check) || !same(before,check)) return {false,false,recovery,"Controller changed before dispatch"};
        if(!initial) initial=before.word; attempted=target; recovery=true;
        bool accepted=write(target); State after;
        if(accepted && capture(after) && after.word==target && before.address==after.address && before.limit==after.limit && before.mode==after.mode) {last=target; attempted.reset(); recovery=false; return {true,true,false,"Offset word read back exactly; physical voltage requires a meter"};}
        if(restore_word(before.word)) {last=before.word; attempted.reset(); recovery=false; if(initial && *initial==before.word) {initial.reset(); last.reset();} return {false,true,false,"Write failed; entry word restored"};}
        return {false,true,true,"Write outcome uncertain; captured word requires restoration"};
    }
public:
    MpOffset(ControllerIo& x,I2cRoute r):io(x),route(r){}
    // Discovery describes the physical candidate independently of the chosen
    // write recipe. E1/manual-mode eligibility is checked by capture() only
    // when reading or operating the offset method.
    bool probe() override {identified=fingerprint(); return identified;}
    ControllerView read(bool xoc) override {
        ControllerView v; v.name="MP2888A offset candidate"; v.owned=bool(initial); v.recovery=recovery; v.verified=verified;
        State s; if(!capture(s)) {v.stale=true; v.message="Offset state unavailable or manual mode active"; return v;}
        v.readable=v.capable=true; I2cField f; f.key="offset"; f.label="Offset"; f.unit="mV"; f.value=sign(s.word)*6.25; f.minimum=xoc?-111*6.25:-200; f.maximum=xoc?112*6.25:100; f.step=6.25; f.writable=verified&&!recovery; f.resettable=true; f.reset_label="Stock / zero"; f.detail="Controller offset command; physical voltage requires a meter"; v.fields.push_back(f); v.readings.emplace_back("Controller-reported VOUT",std::to_string(s.voltage)+" mV"); v.message="Candidate fingerprint; electrical rail is unassigned"; return v;
    }
    I2cResult apply(const ControllerRequest& r,const ControllerContext& c) override {if(!c.gate || !verified) return {false,false,recovery,"Unlock controls and verify offset response first"}; if(recovery) return {false,false,true,"Restore uncertain offset first"}; if(r.key!="offset") return {false,false,recovery,"Unknown field"}; return set(r.reset?0:r.value,c.xoc);}
    I2cResult restore() override {if(!initial) return {true,false,false,"No owned offset"}; if(!restore_word(*initial)) {recovery=true; return {false,true,true,"Captured offset restoration is unconfirmed"};} initial.reset();last.reset();attempted.reset();recovery=false;return {true,true,false,"Captured offset word restored"};}
    bool owned() const override {return bool(initial);}
    I2cResult verify(const ControllerContext& c) override;
};
I2cResult MpOffset::verify(const ControllerContext& c) {
    verified=false; if(!c.gate || initial || recovery) return {false,false,recovery,"Verification requires unlocked controls and no owned offset"};
    auto point=io.operating_point(); if(!point || point->pstate!=0 || !point->core_mhz || !point->memory_mhz || !std::isfinite(*point->core_mhz) || !std::isfinite(*point->memory_mhz) || *point->core_mhz<=0 || *point->memory_mhz<=0) return {false,false,false,"Stable P0 and both clocks are required"};
    auto stable=[&](){auto p=io.operating_point(); return p && p->pstate==0 && p->core_mhz==point->core_mhz && p->memory_mhz==point->memory_mhz;};
    struct Sample {double median=0,noise=0;};
    auto sample=[&](unsigned word,Sample& out) {std::vector<double> values; for(int i=0;i<25;++i) {State s; if(!stable() || !capture(s) || s.word!=word) return false; values.push_back(s.voltage); if(!stable()) return false; io.sleep_ms(40);} if(!stable()) return false; std::sort(values.begin(),values.end());out={values[12],values.back()-values.front()};return true;};
    State entry; Sample base; if(!capture(entry) || !sample(entry.word,base)) return {false,false,false,"Baseline unavailable; nothing written"};
    bool changed=false, hit=false, failed=false; Sample trial; double hit_noise=0;
    for(double rung:{6.25,12.5,25.0,50.0,75.0}) {
        if(!stable()) {failed=true;break;}
        auto result=set(sign(entry.word)*6.25+rung,c.xoc); changed|=result.may_have_changed;
        if(!result.ok) {failed=true;break;}
        unsigned expected=unsigned(sign(entry.word)+int(std::lround(rung/6.25)))&255; io.sleep_ms(150);
        if(!sample(expected,trial)) {failed=true;break;}
        hit_noise=std::max(base.noise,trial.noise); double delta=trial.median-base.median;
        if(delta>=6.25 && delta>hit_noise) {hit=true;break;}
    }
    if(initial) {auto result=restore(); if(!result.ok) return {false,changed,true,"Verification restoration failed; captured word retained"};}
    State final; if(!capture(final,false) || final.word!=entry.word) {initial=entry.word; recovery=true; return {false,changed,true,"Entry offset readback unavailable or changed"};}
    if(failed || !hit) return {false,changed,false,"Offset response inconclusive; entry word restored"};
    io.sleep_ms(150); Sample back; bool sampled=sample(entry.word,back);
    if(!capture(final,false) || final.word!=entry.word) {initial=entry.word;recovery=true;return {false,changed,true,"Final entry readback failed"};}
    if(!sampled || std::abs(back.median-base.median)>std::max(7.0,std::max(base.noise,back.noise)) || trial.median-back.median<6.25 || trial.median-back.median<=std::max(hit_noise,back.noise)) return {false,changed,false,"Offset response reversal inconclusive; entry word restored"};
    verified=true; return {true,changed,false,"Controller VOUT response and reversal verified at held operating point; entry restored"};
}
}
std::unique_ptr<ControllerDriver> make_mp2888_offset(ControllerIo& io,I2cRoute r) {return std::make_unique<MpOffset>(io,r);}
}
