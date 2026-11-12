/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "i2c_driver.h"
#include <cmath>
#include <algorithm>
#include <vector>

namespace druta::ui {
namespace {
class Ncp final : public ControllerDriver {
    ControllerIo& io; I2cRoute route;
    struct State {uint8_t command=0, a=0, b=0; bool enabled() const {return (a&8)!=0;} bool operator==(const State& s) const {return command==s.command && a==s.a && b==s.b;} };
    std::optional<unsigned> revision; std::optional<State> initial, last; std::vector<State> permitted; bool recovery=false, verified=false;
    bool get(uint8_t reg,size_t n,unsigned& v) {std::vector<uint8_t> b; for(int i=0;i<2;++i) if(io.read_bytes(route,reg,n,b) && b.size()==n) {v=0; for(size_t j=0;j<n;++j) v|=unsigned(b[j])<<(8*j); return true;} return false;}
    bool identity() {unsigned a,m,r,mode; if(!io.controller_bound() || route.port>=8 || route.addr7<8 || route.addr7>0x77 || !get(0x99,1,a) || a!=0x41 || !get(0x9a,2,m) || (m!=0x3298 && m!=0x0208) || !get(0x9b,1,r) || !get(0x20,1,mode) || mode!=32) return false; unsigned id=(m<<8)|r; if(revision && *revision!=id) return false; revision=id; return true;}
    bool capture_raw(State& s) {unsigned cmd,a,b; if(!identity() || !get(0x21,2,cmd) || cmd>255 || !get(0xd2,1,a) || !get(0xd3,1,b)) return false; s={uint8_t(cmd),uint8_t(a),uint8_t(b)}; return true;}
    bool capture(State& s) {return capture_raw(s) && !((s.a^s.b)&8) && (!(s.a&8) || (s.command>=2 && s.command<=198));}
    bool write_checked(uint8_t reg,unsigned value,size_t n) {std::vector<uint8_t> b(n); for(size_t i=0;i<n;++i) b[i]=uint8_t(value>>(8*i)); if(!io.write_bytes(I2cFamily::ncp4206,route,reg,b)) return false; unsigned actual; return get(reg,n,actual) && actual==value;}
    bool known(const State& state) const {return std::find(permitted.begin(),permitted.end(),state)!=permitted.end();}
    bool set(const State& target) {
        State current; if(!capture_raw(current) || !known(current)) return false;
        auto dispatch=[&](uint8_t reg, uint8_t value, size_t width) {
            State before; if(!capture_raw(before) || !(before==current)) return false;
            State next=before; if(reg==0x21) next.command=value; else if(reg==0xd2) next.a=value; else next.b=value;
            if(next==before) return true;
            permitted={before,next}; // record both possible outcomes before dispatch
            bool ok=write_checked(reg,value,width);
            State after; if(!ok || !capture_raw(after) || !(after==next)) return false;
            current=next; permitted={next}; return true;
        };
        bool enable=target.enabled();
        if(enable && !dispatch(0x21,target.command,2)) return false;
        if(!dispatch(0xd2,enable?uint8_t(current.a|8):uint8_t(current.a&~8),1)) return false;
        if(!dispatch(0xd3,enable?uint8_t(current.b|8):uint8_t(current.b&~8),1)) return false;
        if(!enable && !dispatch(0x21,target.command,2)) return false;
        State after; return capture(after) && after.command==target.command && (after.a&8)==(target.a&8) && (after.b&8)==(target.b&8);
    }
    I2cResult recover_auto() {
        State before; if(!capture_raw(before)) return {false,false,recovery,"Auto recovery cannot read identity and configuration"};
        if(recovery && !known(before)) return {false,false,true,"Auto recovery state differs from known dispatch states"};
        if(!recovery) permitted={before};
        State target=before; target.a &= ~8; target.b &= ~8;
        recovery=true;
        if(!set(target)) return {false,true,true,"Auto recovery failed; state uncertain"};
        permitted.clear(); recovery=false;
        return {true,true,false,"GPU VID mode restored; command left unchanged"};
    }
    I2cResult transition(const State& before,const State& target) {
        State now; if(!capture(now) || !(now==before)) return {false,false,recovery,"Controller state changed before write"};
        if(before==target) return {true,false,recovery,"Command already matches"};
        if(!initial) initial=before;
        permitted={before};
        if(set(target)) {last=target; return {true,true,recovery,"Command and paired mode read back exactly"};}
        recovery=true; last.reset();
        if(set(before) && capture(now) && now==before) {last=before; recovery=false; if(initial && before==*initial) {initial.reset(); last.reset(); permitted.clear();} return {false,true,false,"Write failed; entry command and mode restored"};}
        return {false,true,true,"Write or rollback failed; restoration pending"};
    }
public:
    Ncp(ControllerIo& x,I2cRoute r):io(x),route(r){}
    bool probe() override {State s; return capture(s);}
    ControllerView read(bool xoc) override {
        ControllerView v; v.name="NCP4206"; v.owned=bool(initial); v.recovery=recovery; v.verified=verified; v.needs_verification=true;
        State s; if(!capture(s)) {v.stale=true; v.message="Controller state unavailable or paired modes disagree"; return v;}
        v.readable=v.capable=true; I2cField f; f.key="voltage"; f.label="Voltage target"; f.unit="mV"; if(s.enabled()) f.value=1600.0-(s.command-2)*6.25; f.minimum=600; f.maximum=xoc?1600:1281; f.step=6.25; f.writable=verified; f.resettable=true; f.reset_label="Auto"; f.detail="VID command; physical rail unassigned"; v.fields.push_back(f); v.message=s.enabled()?"Absolute VID override":"GPU VID mode (Auto)"; return v;
    }
    I2cResult verify(const ControllerContext& context) override {
        verified=false;
        if(!context.gate || recovery || initial) return {false,false,recovery,"Verification requires unlocked controls and no owned or uncertain request"};
        auto point=io.operating_point();
        if(!point || point->pstate!=0 || !point->core_mhz || !point->memory_mhz || !std::isfinite(*point->core_mhz) || !std::isfinite(*point->memory_mhz) || *point->core_mhz<=0 || *point->memory_mhz<=0) return {false,false,recovery,"Stable P0 core and memory clocks are required"};
        auto stable=[&]() {auto p=io.operating_point(); return p && p->pstate==0 && p->core_mhz==point->core_mhz && p->memory_mhz==point->memory_mhz;};
        struct Sample {double median=0, noise=0, quantum=0;};
        auto sample=[&](const State& expected, Sample& out, bool ceiling) {
            std::vector<double> values; double quantum=0;
            for(int i=0;i<25;++i) {
                State s; unsigned raw;
                if(!stable() || !capture(s) || !(s==expected) || !get(0xd7,2,raw)) return false;
                int exponent=(raw>>11)&31; if(exponent&16) exponent-=32;
                int mantissa=raw&2047; if(mantissa&1024) mantissa-=2048;
                double q=std::ldexp(1000.0,exponent), value=mantissa*q;
                if(!std::isfinite(value) || value<300 || value>2000 || (ceiling && value>1281)) return false;
                values.push_back(value); quantum=std::max(quantum,q);
                if(!stable()) return false; io.sleep_ms(40);
            }
            if(!stable()) return false; std::sort(values.begin(),values.end()); out={values[12],values.back()-values.front(),quantum}; return true;
        };
        State entry; Sample base; if(!capture(entry) || !sample(entry,base,false)) return {false,false,false,"Verification baseline unavailable; nothing written"};
        bool attempted=false, hit=false, failed=false; Sample trial; double used=0;
        for(double offset : {25.0,37.5,50.0}) {
            double target=std::floor((base.median+offset)/6.25)*6.25;
            if(target<600 || target>1281 || target<=base.median) continue;
            if(!stable()) {failed=true; break;}
            State requested=entry; requested.command=uint8_t(2+std::ceil((1600-target)/6.25)); requested.a|=8; requested.b|=8;
            attempted=true; if(!initial) {initial=entry; permitted={entry};} recovery=true; last.reset();
            if(!set(requested)) {failed=true; break;} last=requested;
            io.sleep_ms(150);
            if(!sample(requested,trial,true)) {failed=true; break;}
            double delta=trial.median-base.median;
            if(delta>=std::max(6.25,std::max(base.quantum,trial.quantum)) && delta>std::max(base.noise,trial.noise)) {hit=true; used=delta; break;}
        }
        if(!attempted) return {false,false,false,"Verification lacks bounded voltage headroom; nothing written"};
        State restored;
        if(!set(entry) || !capture(restored) || !(restored==entry)) {recovery=true; last.reset(); return {false,true,true,"Verification restoration failed; state uncertain"};}
        initial.reset(); last.reset(); permitted.clear(); recovery=false;
        if(failed || !hit) return {false,true,false,"Verification inconclusive; entry command and mode restored"};
        io.sleep_ms(150); Sample back;
        bool sampled=sample(entry,back,false);
        if(!capture(restored) || !(restored==entry)) { initial=entry; last.reset(); permitted={entry}; recovery=true; return {false,true,true,"Verification final state unavailable or changed; restoration pending"}; }
        if(!sampled) return {false,true,false,"Verification reversal unavailable; entry state restored"};
        double q=std::max(base.quantum,back.quantum);
        double tolerance=std::max(std::ceil(6.25/q)*q,std::max(base.noise,back.noise));
        double reversal=trial.median-back.median;
        if(std::abs(back.median-base.median)>tolerance || reversal<std::max(6.25,std::max(trial.quantum,back.quantum)) || reversal<=std::max(std::max(base.noise,trial.noise),back.noise)) return {false,true,false,"Verification response or reversal inconclusive; entry state restored"};
        (void)used; verified=true; return {true,true,false,"VMON response and reversal verified at the held operating point; entry state restored"};
    }
    I2cResult apply(const ControllerRequest& r,const ControllerContext& c) override {
        if(!c.gate) return {false,false,recovery,"Unlock controls and enable I2C opt-in"};
        if(r.key!="voltage") return {false,false,recovery,"Unknown field"};
        if(recovery && !r.reset) return {false,false,true,"Restore uncertain state first"};
        State s; if(!capture(s)) return r.reset ? (initial ? restore() : recover_auto()) : I2cResult{false,false,recovery,"Controller state unavailable"};
        if(recovery && r.reset) return initial ? restore() : recover_auto();
        if(last && !(s==*last)) return {false,false,true,"Owned command changed externally"};
        if(!r.reset && !verified) return {false,false,recovery,"Response verification required"};
        State target=s;
        if(r.reset) {target.a &= ~8; target.b &= ~8; target.command=0;}
        else {double max=c.xoc?1600:1281; if(!std::isfinite(r.value) || r.value<600 || r.value>max) return {false,false,recovery,"Target outside VID range"}; target.command=uint8_t(2+std::ceil((1600-r.value)/6.25)); target.a|=8; target.b|=8;}
        return transition(s,target);
    }
    I2cResult restore() override {
        if(!initial) {if(recovery) return recover_auto(); return {true,false,false,"No owned command"};}
        State current; if(!capture_raw(current) || !known(current)) {recovery=true; return {false,false,true,"Controller state unavailable or differs from known dispatch states"};}
        if(!set(*initial)) {recovery=true; last.reset(); return {false,true,true,"Restore failed; command may have changed"};}
        initial.reset(); last.reset(); permitted.clear(); recovery=false; return {true,true,false,"Entry command and mode restored"};
    }
    bool owned() const override {return bool(initial);}
};
}
std::unique_ptr<ControllerDriver> make_ncp4206(ControllerIo& io,I2cRoute r) {return std::make_unique<Ncp>(io,r);}
}
