/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "i2c_driver.h"
#include <array>
#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace druta::ui {
struct Nct3933PlanState {
    std::shared_ptr<int> session;
    Nct3933State before,desired;
    uint8_t selected=0;
    uint64_t epoch=0;
};
struct Nct3933ReceiptState {
    std::shared_ptr<int> session;
    std::shared_ptr<const Nct3933PlanState> plan;
    Nct3933State before,desired;
    uint8_t issued=0;
    uint64_t epoch=0;
};
namespace {
class Nct final : public ControllerDriver {
    ControllerIo& io; I2cRoute route;
    struct State { std::array<uint8_t,3> out{}; uint8_t config=0; bool operator==(const State& b) const { return out==b.out && config==b.config; } };
    std::array<std::optional<uint8_t>,3> initial{}, last{};
    std::array<std::vector<uint8_t>,3> permitted{};
    std::optional<uint8_t> owned_config;
    bool recovery=false, verified=false;
    std::shared_ptr<int> session=std::make_shared<int>(0);
    std::shared_ptr<Nct3933ReceiptState> profile;
    uint64_t epoch=0;
    bool byte(uint8_t reg, uint8_t& v) { std::vector<uint8_t> b; for(int i=0;i<2;++i) if(io.read_bytes(route,reg,1,b) && b.size()==1) {v=b[0]; return true;} return false; }
    bool identity() { uint8_t a,b; return io.controller_bound() && route.port<8 && route.addr7>=0x10 && route.addr7<=0x15 && byte(0x5d,a) && a==0x39 && byte(0x5e,b) && b==0x33; }
    bool capture(State& s) { uint8_t c; return identity() && byte(5,s.config) && byte(1,s.out[0]) && byte(2,s.out[1]) && byte(3,s.out[2]) && byte(5,c) && c==s.config && identity(); }
    bool has_owned() const { for(auto& x:initial) if(x) return true; return false; }
    static void require(bool condition,const char* message) { if(!condition)throw std::runtime_error(message); }
    Nct3933State profile_capture() { State a,b; require(capture(a)&&capture(b)&&a==b,"Stable NCT3933 identity/configuration/output readback unavailable");return {a.out,a.config}; }
    static bool selected_equal(const Nct3933State& a,const Nct3933State& b,uint8_t mask) { if(a.configuration!=b.configuration)return false;for(size_t i=0;i<3;++i)if((mask&(1u<<i))&&a.outputs[i]!=b.outputs[i])return false;return true; }
    void qualify(const Nct3933Plan& plan) { require(plan.state&&plan.state->session==session,"NCT3933 plan belongs to another selected driver");require(plan.state->epoch==epoch&&!profile&&!has_owned()&&!recovery,"NCT3933 ownership changed since preflight"); }
    void qualify(const Nct3933Receipt& receipt) { require(receipt.state&&receipt.state->session==session&&receipt.state->issued,"NCT3933 receipt belongs to another driver or has no issued outputs");require(receipt.state->epoch==epoch&&(!profile||profile==receipt.state),"NCT3933 owned lifecycle was replaced"); }
    void candidates(const Nct3933ReceiptState& receipt,const Nct3933State& current) { require(current.configuration==receipt.before.configuration,"NCT3933 configuration/scaling changed; no restore issued");for(size_t i=0;i<3;++i)if(receipt.issued&(1u<<i))require(current.outputs[i]==receipt.before.outputs[i]||current.outputs[i]==receipt.desired.outputs[i],"An issued NCT3933 output changed externally; no restore issued"); }
    I2cResult change(int i, uint8_t value) {
        State before, check; if(!capture(before)) return {false,false,recovery,"NCT3933U state unavailable"};
        if(owned_config && *owned_config!=before.config) return {false,false,true,"Configuration changed; restore unavailable"};
        if(last[i] && *last[i]!=before.out[i]) return {false,false,true,"Owned output changed externally"};
        if(before.out[i]==value) return {true,false,recovery,"Output already matches"};
        if(!capture(check) || !(check==before)) return {false,false,recovery,"State changed before write"};
        if(!initial[i]) initial[i]=before.out[i]; owned_config=before.config;
        permitted[i]={before.out[i],value};
        ++epoch;bool ok=io.write_bytes(I2cFamily::nct3933,route,uint8_t(i+1),{value});
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
        ControllerView v; v.name="NCT3933U"; v.owned=owned(); v.recovery=recovery; v.needs_verification=false;
        State s; if(!capture(s)) {v.stale=true; v.message="Controller state unavailable"; return v;}
        v.readable=v.capable=true; v.verified=verified;
        for(int i=0;i<3;++i) { int step=(s.config & (1<<(2*i)))?20:10; int mag=(s.out[i]&127)*step; I2cField f; f.key="out"+std::to_string(i+1); f.label="OUT"+std::to_string(i+1); f.unit="µA"; f.value=(s.out[i]&128)?mag:-mag; f.minimum=-127*step; f.maximum=127*step; f.step=step; f.writable=true; f.resettable=true; f.reset_label="Zero"; f.detail="Nominal current; board voltage effect unknown"; v.fields.push_back(f); }
        v.message=(s.config&0x40)?"Outputs disabled by power saving":"Nominal current commands; physical voltage unknown"; return v;
    }
    I2cResult verify(const ControllerContext&) override { State s; verified=capture(s); return {verified,false,recovery,verified?"Identity and settings read successfully; physical voltage unknown":"Identity or settings unavailable"}; }
    I2cResult apply(const ControllerRequest& r,const ControllerContext& c) override {
        if(profile)return {false,false,recovery,"Restore the owned exact NCT3933 profile before scalar edits"};
        if(!c.gate) return {false,false,recovery,"Unlock controls and enable I2C opt-in"};
        if(recovery) return {false,false,true,"Restore uncertain state first"};
        int i=r.key=="out1"?0:r.key=="out2"?1:r.key=="out3"?2:-1;
        if(i<0) return {false,false,recovery,"Unknown output"}; State s; if(!capture(s)) return {false,false,recovery,"Controller state unavailable"};
        int step=(s.config & (1<<(2*i)))?20:10; double x=r.reset?0:r.value; double n=std::abs(x)/step;
        if(!std::isfinite(x) || n>127 || std::abs(n-std::round(n))>1e-9) return {false,false,recovery,"Current is outside the supported step or range"};
        return change(i,uint8_t(std::lround(n)|(x>0?128:0)));
    }
    I2cResult restore() override {
        if(profile)return restore_nct3933({profile});
        bool changed=false; for(int i=0;i<3;++i) if(initial[i]) {
            State s; if(!capture(s) || !owned_config || s.config!=*owned_config || std::find(permitted[i].begin(),permitted[i].end(),s.out[i])==permitted[i].end()) return {false,changed,true,"Owned output cannot be safely restored"};
            if(s.out[i]!=*initial[i]) { auto target=*initial[i]; permitted[i].push_back(target); ++epoch;if(!io.write_bytes(I2cFamily::nct3933,route,uint8_t(i+1),{target})) {recovery=true; return {false,true,true,"Restore write uncertain"};} changed=true; State expected=s; expected.out[i]=target; if(!capture(s) || !(s==expected)) {recovery=true; return {false,true,true,"Restore readback mismatch"};} }
            initial[i].reset(); last[i].reset(); permitted[i].clear();
        } recovery=false; owned_config.reset(); return {true,changed,false,"Owned outputs restored"};
    }
    bool owned() const override {return has_owned()||(profile&&profile->issued);}
    I2cResult capture_nct3933(Nct3933State& out) override {try{out=profile_capture();return {true,false,recovery,"Exact NCT3933 output bytes and configuration read"};}catch(const std::exception& e){return {false,false,recovery,e.what()};}}
    I2cResult preview_nct3933(const Nct3933State& desired,uint8_t selected,Nct3933Plan& out) override {
        out={};try{require(selected&&!(selected&~7u),"Select understood NCT3933 outputs");require(!owned()&&!recovery,"Release existing NCT3933 requests before profile preflight");const auto before=profile_capture();require(before.configuration==desired.configuration,"Saved NCT3933 configuration/scaling differs; configuration is never replayed");
            auto plan=std::make_shared<Nct3933PlanState>();plan->session=session;plan->before=before;plan->desired=desired;plan->selected=selected;plan->epoch=epoch;out.state=std::move(plan);return {true,false,false,"Selected exact DAC requests preflighted without writes"};
        }catch(const std::exception& e){return {false,false,recovery,e.what()};}
    }
    I2cResult apply_nct3933(const Nct3933Plan& plan,const ControllerContext& context,const std::function<void(const Nct3933Receipt&)>& journal) override {
        I2cResult result;Nct3933Receipt receipt;
        try{require(context.gate,"Enable I2C opt-in before applying exact DAC requests");qualify(plan);const auto& frozen=*plan.state;auto current=profile_capture();require(selected_equal(current,frozen.before,frozen.selected),"Selected NCT3933 requests changed after preflight");
            receipt.state=std::make_shared<Nct3933ReceiptState>();receipt.state->session=session;receipt.state->plan=plan.state;receipt.state->before=frozen.before;receipt.state->desired=frozen.desired;receipt.state->epoch=epoch;uint8_t completed=0;
            for(size_t i=0;i<3;++i)if(frozen.selected&(1u<<i)){
                current=profile_capture();require(current.configuration==frozen.before.configuration,"NCT3933 configuration changed during Apply");for(size_t n=0;n<3;++n)if(frozen.selected&(1u<<n))require(current.outputs[n]==((completed&(1u<<n))?frozen.desired.outputs[n]:frozen.before.outputs[n]),"Selected NCT3933 request changed before its write");
                if(current.outputs[i]!=frozen.desired.outputs[i]){const bool first=!receipt.state->issued;receipt.state->issued|=uint8_t(1u<<i);if(first){try{if(journal)journal(receipt);}catch(...){receipt.state->issued=0;throw;}}profile=receipt.state;++epoch;receipt.state->epoch=epoch;recovery=true;result.may_have_changed=true;
                    const bool ok=io.write_bytes(I2cFamily::nct3933,route,uint8_t(i+1),{frozen.desired.outputs[i]});auto expected=current;expected.outputs[i]=frozen.desired.outputs[i];const auto after=profile_capture();require(ok&&selected_equal(after,expected,7),"NCT3933 output write/readback unverified; issued originals retained");}
                completed|=uint8_t(1u<<i);
            }
            require(selected_equal(profile_capture(),frozen.desired,frozen.selected),"Final selected NCT3933 requests differ; issued originals retained");recovery=false;result.ok=true;result.message="Exact selected DAC bytes verified; configuration and other outputs preserved";
        }catch(const std::exception& e){result.message=e.what();}result.recovery_pending=recovery;return result;
    }
    I2cResult verify_nct3933(const Nct3933Plan& plan) override {try{require(plan.state&&plan.state->session==session,"NCT3933 plan belongs to another driver");require((profile&&profile->plan==plan.state&&profile->epoch==epoch)||(!profile&&plan.state->epoch==epoch),"NCT3933 lifecycle changed");require(!recovery&&selected_equal(profile_capture(),plan.state->desired,plan.state->selected),"Selected NCT3933 desired bytes unavailable or differ");return {true,false,false,"Selected exact DAC requests verified"};}catch(const std::exception& e){return {false,false,recovery,e.what()};}}
    I2cResult verify_nct3933(const Nct3933Receipt& receipt,bool original=false) override {try{qualify(receipt);require(!recovery&&selected_equal(profile_capture(),original?receipt.state->before:receipt.state->desired,receipt.state->issued),"Issued NCT3933 bytes unavailable or differ");return {true,false,false,"Issued exact DAC bytes verified"};}catch(const std::exception& e){return {false,false,recovery,e.what()};}}
    I2cResult restore_nct3933(const Nct3933Receipt& receipt) override {
        I2cResult result;try{qualify(receipt);auto& saved=*receipt.state;candidates(saved,profile_capture());
            for(size_t i=0;i<3;++i)if(saved.issued&(1u<<i)){const auto before=profile_capture();candidates(saved,before);if(before.outputs[i]==saved.before.outputs[i])continue;
                profile=receipt.state;++epoch;saved.epoch=epoch;recovery=true;result.may_have_changed=true;const bool ok=io.write_bytes(I2cFamily::nct3933,route,uint8_t(i+1),{saved.before.outputs[i]});auto expected=before;expected.outputs[i]=saved.before.outputs[i];require(ok&&selected_equal(profile_capture(),expected,7),"NCT3933 restoration write/readback unverified; receipt retained");}
            require(selected_equal(profile_capture(),saved.before,saved.issued),"Final issued NCT3933 original bytes differ");if(profile==receipt.state)profile.reset();recovery=false;result.ok=true;result.message="Issued original DAC bytes restored; current unselected outputs preserved";
        }catch(const std::exception& e){result.message=e.what();}result.recovery_pending=recovery;return result;
    }
};
}
std::unique_ptr<ControllerDriver> make_nct3933(ControllerIo& io,I2cRoute r) {return std::make_unique<Nct>(io,r);}
}
