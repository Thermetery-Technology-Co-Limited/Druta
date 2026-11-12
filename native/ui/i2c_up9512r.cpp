/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "i2c_driver.h"
#include <algorithm>
#include <array>
#include <cmath>
#include <set>
namespace druta::ui {
namespace {
using Raw = std::array<uint8_t,4>;
constexpr uint8_t regs[] = {0x0a,0x0b,0x0c,0x2a};
constexpr uint8_t masks[] = {0xff,0xff,0xf0,0x40};
class Up final : public ControllerDriver {
    ControllerIo& io_; I2cRoute route_; bool verified_=false, owned_=false, pending_=false;
    Raw initial_{}; std::set<Raw> accepted_;
    bool byte(uint8_t reg, uint8_t& v, bool retry=true) {
        for (int n=0;n<(retry?3:1);++n) { if(n) io_.sleep_ms(20); std::vector<uint8_t> b;
            if (!io_.controller_bound()) return false;
            if (io_.read_bytes(route_,reg,1,b)) { if(b.size()!=1 || !io_.controller_bound()) return false; v=b[0]; return true; }
        } return false;
    }
    bool identity(bool retry=true) {
        if (route_.port>7 || route_.addr7<8 || route_.addr7>0x77) return false;
        for (int i=0;i<2;++i) { uint8_t a,b; if (!byte(0x27,a,retry)||!byte(0x28,b,retry)||a!=0||b!=0x2b) return false; } return true;
    }
    bool capture(Raw& r) {
        if(!identity()) return false; Raw again{};
        for(int i=0;i<4;++i) if(!byte(regs[i],r[i])) return false;
        for(int i=0;i<4;++i) if(!byte(regs[i],again[i])) return false;
        return r==again && identity();
    }
    bool writable(const Raw& r) { uint8_t lock; return !(r[3]&0x83) && byte(0x39,lock) && lock==0x94; }
    bool voltage(int& mv) { uint8_t b; if(!identity()||!byte(0x2d,b)||!identity()) return false; mv=10*b; return true; }
    static std::array<int,5> offsets(const Raw& r) { return {10*(r[0]>>4),10*(r[0]&15),10*(r[1]>>4),10*(r[1]&15),10*(r[2]>>4)}; }
    static Raw encode(const Raw& before, const std::array<int,5>& a, bool enable) {
        return {uint8_t((a[0]/10<<4)|(a[1]/10)),uint8_t((a[2]/10<<4)|(a[3]/10)),uint8_t((a[4]/10<<4)|(before[2]&15)),uint8_t((before[3]&~0x40)|(enable?0x40:0))};
    }
    bool same_unknown(const Raw& a,const Raw& b) { for(int i=0;i<4;++i) if((a[i]&~masks[i])!=(b[i]&~masks[i])) return false; return true; }
    bool dispatch(Raw current,const Raw& target) {
        const int normal[]={0,1,2,3}, disable[]={3,0,1,2}; const int* order=(target[3]&0x40)?normal:disable;
        for(int n=0;n<4;++n) { int i=order[n]; if(current[i]==target[i]) continue; Raw check;
            if(!capture(check)||check!=current||!writable(current)) return false;
            Raw next=current; next[i]=target[i]; accepted_.insert(current); accepted_.insert(next); pending_=true;
            if(!io_.write_bytes(I2cFamily::up9512r,route_,regs[i],{target[i]})) return false;
            if(!capture(check)||check!=next) return false; current=next; accepted_.clear(); accepted_.insert(current);
        }
        Raw check; return capture(check)&&check==target && writable(check);
    }
    bool restore_entry() {
        for(int attempt=0;attempt<3;++attempt) { if(attempt) io_.sleep_ms(50); Raw current;
            if(!capture(current)||!same_unknown(current,initial_)||!accepted_.count(current)||!writable(current)) continue;
            if(current==initial_ || dispatch(current,initial_)) { Raw check; if(capture(check)&&check==initial_) { pending_=false; return true; } }
        } pending_=true; return false;
    }
    bool point() { auto p=io_.operating_point(); return p && p->pstate==0; }
    bool sample(const Raw& expected,int ceiling,int& med,int& noise) {
        std::array<int,25> values{};
        for(auto& v:values) { Raw current; if(!point()||!capture(current)||current!=expected||!voltage(v)||v<300||v>ceiling||!point()) return false; io_.sleep_ms(40); }
        if(!point()) return false; std::sort(values.begin(),values.end()); med=values[12]; noise=values.back()-values.front(); return true;
    }
public:
    Up(ControllerIo& io,I2cRoute route):io_(io),route_(route) {}
    bool probe() override { return identity(false); }
    ControllerView read(bool xoc) override {
        ControllerView v; v.name="uP9512R offsets"; v.verified=verified_; v.owned=owned_; v.recovery=pending_;
        Raw r; if(!capture(r)) { v.stale=true; v.message="Controller state temporarily unavailable or identity changed"; return v; }
        v.readable=true; v.capable=!(r[3]&0x83); auto a=offsets(r);
        I2cField f; f.key="offset"; f.label="All five load-current offsets"; f.unit="mV"; f.minimum=0; f.maximum=xoc?150:50; f.step=10; f.writable=v.capable; f.resettable=true;
        if(std::all_of(a.begin(),a.end(),[&](int n){return n==a[0];})) f.value=a[0]; else f.detail="Five states differ";
        v.fields.push_back(f); v.readings.push_back({"Offset enable",(r[3]&0x40)?"Enabled":"Disabled"});
        uint8_t b; if(byte(0x2d,b)) v.readings.push_back({"FB ADC",std::to_string(int(b)*10)+" mV"}); if(byte(0x2c,b)) v.readings.push_back({"IMON ADC",std::to_string(int(b)*10)+" mV"});
        if(byte(0x39,b)) v.readings.push_back({"SMBus lock",b==0x94?"Unlocked":"Locked"});
        v.message="FB and IMON are ADC input voltages, not calibrated rail telemetry."; return v;
    }
    I2cResult apply(const ControllerRequest& req,const ControllerContext& ctx) override {
        if(req.key!="offset"||(!ctx.gate&&!req.reset)) return {false,false,pending_,"Offset request requires acknowledgment"};
        if(!req.reset && (!verified_||!std::isfinite(req.value)||req.value<0||req.value>(ctx.xoc?150:50)||std::floor(req.value)!=req.value||int(req.value)%10))
            return {false,false,pending_,"Verify first and use an offset in the allowed 10 mV steps"};
        Raw before; if(!capture(before)||!writable(before)) return {false,false,pending_,"Controller state or lock unavailable, locked, or unknown"};
        if(owned_ && !accepted_.count(before)) return {false,false,true,"Controller changed outside this session"};
        int value=req.reset?0:int(req.value); std::array<int,5> target_values={value,value,value,value,value}; Raw target=encode(before,target_values,!req.reset);
        if(!req.reset) { int mv; if(!voltage(mv)||mv<300||mv>(ctx.xoc?2000:1200)) return {false,false,pending_,"FB ADC unavailable or outside software guard"};
            auto old=offsets(before); int increase=0; for(int x:old) increase=std::max(increase,value-((before[3]&0x40)?x:0));
            if(mv+increase>(ctx.xoc?2000:1200)) return {false,false,pending_,"Offset exceeds FB ADC software ceiling"}; }
        if(before==target) return {true,false,pending_,"Offset request already matches"};
        if(!owned_) { initial_=before; owned_=true; accepted_.clear(); accepted_.insert(before); }
        if(!dispatch(before,target)) { bool restored=restore_entry(); if(restored) owned_=false; return {false,true,!restored,restored?"Write failed; exact captured state restored":"Write uncertain; restoration pending"}; }
        if(!req.reset) { int mv; Raw check; if(!voltage(mv)||mv<300||mv>(ctx.xoc?2000:1200)||!capture(check)||check!=target) { bool restored=restore_entry(); if(restored) owned_=false; return {false,true,!restored,"Post-write check failed; restoration attempted"}; } }
        pending_=false; return {true,true,false,"All five offset fields and enable read back exactly"};
    }
    I2cResult restore() override { if(!owned_) return {true,false,false,"No owned offset request"}; bool ok=restore_entry(); if(ok) owned_=false; return {ok,true,!ok,ok?"Exact captured offset state restored":"Offset restoration pending; state unavailable or changed"}; }
    I2cResult verify(const ControllerContext& ctx) override {
        if(!ctx.gate||owned_||!point()) return {false,false,pending_,"Verification requires acknowledgment, a held P0 operating point, and no owned request"};
        Raw original; int base,base_noise,ceiling=ctx.xoc?2000:1200;
        if(!capture(original)||!writable(original)||!sample(original,ceiling,base,base_noise)) return {false,false,false,"Baseline unavailable; nothing written"};
        auto effective=offsets(original); if(!(original[3]&0x40)) effective.fill(0); int max=*std::max_element(effective.begin(),effective.end());
        initial_=original; accepted_.clear(); accepted_.insert(original); owned_=true; Raw current=original; bool hit=false,failed=false; int trial=0,noise=0;
        for(int step=10;step<=50;step+=10) {
            if(max+step>(ctx.xoc?150:50)||base+step>ceiling) break;
            auto values=effective; for(auto& v:values) v+=step; Raw target=encode(original,values,true); int mv;
            if(!point()||!voltage(mv)||mv<300||mv>ceiling||mv+10>ceiling||!dispatch(current,target)) { failed=true; break; }
            current=target; io_.sleep_ms(150);
            if(!sample(current,ceiling,trial,noise)) { failed=true; break; }
            if(trial-base>=10 && trial-base>std::max(base_noise,noise)) { hit=true; break; }
        }
        bool restored=restore_entry(); if(restored) owned_=false; if(!restored) return {false,true,true,"Verification restoration failed; recovery pending"};
        if(failed||!hit) return {false,true,false,"Verification inconclusive; exact captured state restored"};
        io_.sleep_ms(150); int final_mv,final_noise; Raw check;
        if(!sample(original,ceiling,final_mv,final_noise)||std::abs(final_mv-base)>std::max({10,base_noise,final_noise})||trial-final_mv<10||!capture(check)||check!=original)
            return {false,true,false,"Verification reversal inconclusive; captured state restored"};
        verified_=true; return {true,true,false,"FB ADC rose and reversed at the held operating point; exact state restored. This is not calibrated gain."};
    }
    bool owned() const override { return owned_; }
};
}
std::unique_ptr<ControllerDriver> make_up9512r(ControllerIo& io,I2cRoute r) { return std::make_unique<Up>(io,r); }
}
