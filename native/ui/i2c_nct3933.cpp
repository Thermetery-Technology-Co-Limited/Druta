/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "i2c_driver.h"
#include <array>
#include <algorithm>
#include <cmath>

namespace druta::ui {
namespace {
class Nct final : public ControllerDriver {
    ControllerIo& io; I2cRoute route;
    struct State { std::array<uint8_t,3> out{}; uint8_t config=0; bool operator==(const State& b) const { return out==b.out && config==b.config; } };
    std::array<std::optional<uint8_t>,3> initial{}, last{};
    std::array<std::vector<uint8_t>,3> permitted{};
    std::optional<uint8_t> owned_config;
    bool recovery=false, verified=false;
    bool byte(uint8_t reg, uint8_t& v) { std::vector<uint8_t> b; for(int i=0;i<2;++i) if(io.read_bytes(route,reg,1,b) && b.size()==1) {v=b[0]; return true;} return false; }
    bool identity() { uint8_t a,b; return io.controller_bound() && route.port<8 && route.addr7>=0x10 && route.addr7<=0x15 && byte(0x5d,a) && a==0x39 && byte(0x5e,b) && b==0x33; }
    bool capture(State& s) { uint8_t c; return identity() && byte(5,s.config) && byte(1,s.out[0]) && byte(2,s.out[1]) && byte(3,s.out[2]) && byte(5,c) && c==s.config && identity(); }
    bool has_owned() const { for(auto& x:initial) if(x) return true; return false; }
    I2cResult change(int i, uint8_t value) {
        State before, check; if(!capture(before)) return {false,false,recovery,"NCT3933U state unavailable"};
        if(owned_config && *owned_config!=before.config) return {false,false,true,"Configuration changed; restore unavailable"};
        if(last[i] && *last[i]!=before.out[i]) return {false,false,true,"Owned output changed externally"};
        if(before.out[i]==value) return {true,false,recovery,"Output already matches"};
        if(!capture(check) || !(check==before)) return {false,false,recovery,"State changed before write"};
        if(!initial[i]) initial[i]=before.out[i]; owned_config=before.config;
        permitted[i]={before.out[i],value};
        bool ok=io.write_bytes(I2cFamily::nct3933,route,uint8_t(i+1),{value});
        State expected=before; expected.out[i]=value;
        if(ok && capture(check) && check==expected) {last[i]=value; permitted[i]={value}; return {true,true,recovery,"Output read back exactly; physical voltage is unknown"};}
        // The write may have reached the device. Restore only our attempted channel,
        // and only when configuration and other channels remain as captured.
        recovery=true;
        if(capture(check) && check.config==before.config) {
            bool others=true; for(int j=0;j<3;++j) if(j!=i && check.out[j]!=before.out[j]) others=false;
            if(others && std::find(permitted[i].begin(),permitted[i].end(),check.out[i])!=permitted[i].end() && (check.out[i]==before.out[i] || (io.write_bytes(I2cFamily::nct3933,route,uint8_t(i+1),{before.out[i]}))) && capture(check) && check==before) {
                last[i]=before.out[i]; permitted[i]={before.out[i]}; recovery=false;
                if(*initial[i]==before.out[i]) { initial[i].reset(); last[i].reset(); permitted[i].clear(); if(!has_owned()) owned_config.reset(); }
                return {false,true,false,"Write failed; entry state restored"};
            }
        }
        last[i].reset(); return {false,true,true,"Write outcome uncertain; restoration pending"};
    }
public:
    Nct(ControllerIo& x,I2cRoute r):io(x),route(r){}
    bool probe() override {State s; return capture(s);}
    ControllerView read(bool) override {
        ControllerView v; v.name="NCT3933U"; v.owned=has_owned(); v.recovery=recovery; v.needs_verification=false;
        State s; if(!capture(s)) {v.stale=true; v.message="Controller state unavailable"; return v;}
        v.readable=v.capable=true; v.verified=verified;
        for(int i=0;i<3;++i) { int step=(s.config & (1<<(2*i)))?20:10; int mag=(s.out[i]&127)*step; I2cField f; f.key="out"+std::to_string(i+1); f.label="OUT"+std::to_string(i+1); f.unit="µA"; f.value=(s.out[i]&128)?mag:-mag; f.minimum=-127*step; f.maximum=127*step; f.step=step; f.writable=true; f.resettable=true; f.reset_label="Zero"; f.detail="Nominal current; board voltage effect unknown"; v.fields.push_back(f); }
        v.message=(s.config&0x40)?"Outputs disabled by power saving":"Nominal current commands; physical voltage unknown"; return v;
    }
    I2cResult verify(const ControllerContext&) override { State s; verified=capture(s); return {verified,false,recovery,verified?"Identity and settings read successfully; physical voltage unknown":"Identity or settings unavailable"}; }
    I2cResult apply(const ControllerRequest& r,const ControllerContext& c) override {
        if(!c.gate) return {false,false,recovery,"Unlock controls and enable I2C opt-in"};
        if(recovery) return {false,false,true,"Restore uncertain state first"};
        int i=r.key=="out1"?0:r.key=="out2"?1:r.key=="out3"?2:-1;
        if(i<0) return {false,false,recovery,"Unknown output"}; State s; if(!capture(s)) return {false,false,recovery,"Controller state unavailable"};
        int step=(s.config & (1<<(2*i)))?20:10; double x=r.reset?0:r.value; double n=std::abs(x)/step;
        if(!std::isfinite(x) || n>127 || std::abs(n-std::round(n))>1e-9) return {false,false,recovery,"Current is outside the supported step or range"};
        return change(i,uint8_t(std::lround(n)|(x>0?128:0)));
    }
    I2cResult restore() override {
        bool changed=false; for(int i=0;i<3;++i) if(initial[i]) {
            State s; if(!capture(s) || !owned_config || s.config!=*owned_config || std::find(permitted[i].begin(),permitted[i].end(),s.out[i])==permitted[i].end()) return {false,changed,true,"Owned output cannot be safely restored"};
            if(s.out[i]!=*initial[i]) { auto target=*initial[i]; permitted[i].push_back(target); if(!io.write_bytes(I2cFamily::nct3933,route,uint8_t(i+1),{target})) {recovery=true; return {false,true,true,"Restore write uncertain"};} changed=true; State expected=s; expected.out[i]=target; if(!capture(s) || !(s==expected)) {recovery=true; return {false,true,true,"Restore readback mismatch"};} }
            initial[i].reset(); last[i].reset(); permitted[i].clear();
        } recovery=false; owned_config.reset(); return {true,changed,false,"Owned outputs restored"};
    }
    bool owned() const override {return has_owned();}
};
}
std::unique_ptr<ControllerDriver> make_nct3933(ControllerIo& io,I2cRoute r) {return std::make_unique<Nct>(io,r);}
}
