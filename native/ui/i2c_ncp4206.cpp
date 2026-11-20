/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "i2c_driver.h"
#include <cmath>
#include <algorithm>
#include <vector>
#include <stdexcept>

namespace druta::ui {
struct Ncp4206Selected {uint8_t command=0;bool a=false,b=false;bool operator==(const Ncp4206Selected& s)const{return command==s.command&&a==s.a&&b==s.b;}};
struct Ncp4206PlanState {
    std::shared_ptr<int> session;Ncp4206State before,desired;bool xoc=false;uint64_t epoch=0;
};
struct Ncp4206ReceiptState {
    std::shared_ptr<int> session;std::shared_ptr<const Ncp4206PlanState> plan;
    std::vector<Ncp4206Selected> candidates;bool issued=false;uint64_t epoch=0;
};
namespace {
class Ncp final : public ControllerDriver {
    ControllerIo& io; I2cRoute route;
    struct State {uint8_t command=0, a=0, b=0; bool enabled() const {return (a&8)!=0;} bool operator==(const State& s) const {return command==s.command && a==s.a && b==s.b;} };
    std::optional<unsigned> revision; std::optional<State> initial, last; std::vector<State> permitted; bool recovery=false, verified=false;
    std::shared_ptr<int> session=std::make_shared<int>(0);
    std::shared_ptr<Ncp4206ReceiptState> profile;uint64_t epoch=0;
    bool get(uint8_t reg,size_t n,unsigned& v) {std::vector<uint8_t> b; for(int i=0;i<2;++i) if(io.read_bytes(route,reg,n,b) && b.size()==n) {v=0; for(size_t j=0;j<n;++j) v|=unsigned(b[j])<<(8*j); return true;} return false;}
    bool identity() {unsigned a,m,r,mode; if(!io.controller_bound() || route.port>=8 || route.addr7<8 || route.addr7>0x77 || !get(0x99,1,a) || a!=0x41 || !get(0x9a,2,m) || (m!=0x3298 && m!=0x0208) || !get(0x9b,1,r) || !get(0x20,1,mode) || mode!=32) return false; unsigned id=(m<<8)|r; if(revision && *revision!=id) return false; revision=id; return true;}
    bool capture_raw(State& s) {unsigned cmd,a,b; if(!identity() || !get(0x21,2,cmd) || cmd>255 || !get(0xd2,1,a) || !get(0xd3,1,b)) return false; s={uint8_t(cmd),uint8_t(a),uint8_t(b)}; return true;}
    bool capture(State& s) {return capture_raw(s) && !((s.a^s.b)&8) && (!(s.a&8) || (s.command>=2 && s.command<=198));}
    bool write_checked(uint8_t reg,unsigned value,size_t n) {std::vector<uint8_t> b(n); for(size_t i=0;i<n;++i) b[i]=uint8_t(value>>(8*i)); ++epoch;if(!io.write_bytes(I2cFamily::ncp4206,route,reg,b)) return false; unsigned actual; return get(reg,n,actual) && actual==value;}
    static void require(bool ok,const char* message){if(!ok)throw std::runtime_error(message);}
    static Ncp4206Selected selected(const State& s){return {s.command,(s.a&8)!=0,(s.b&8)!=0};}
    static Ncp4206Selected selected(const Ncp4206State& s){return {s.command,s.enabled,s.enabled};}
    static Ncp4206State semantic(const State& s){return {s.enabled(),s.command};}
    static bool valid(const Ncp4206State& s,bool xoc){if(!s.enabled)return true;if(s.command<2||s.command>198)return false;const double mv=1600.0-(s.command-2)*6.25;return mv>=600&&mv<=(xoc?1600:1281);}
    State profile_capture(bool paired=true){State a,b;require(capture_raw(a)&&capture_raw(b)&&a==b&&identity(),"Stable NCP4206 identity/command/configuration readback unavailable");
        if(paired)require(!((a.a^a.b)&8)&&(!a.enabled()||(a.command>=2&&a.command<=198)),"NCP4206 paired mode or active VID encoding is malformed");return a;}
    void qualify(const Ncp4206Plan& plan){require(plan.state&&plan.state->session==session,"NCP4206 plan belongs to another selected driver");require(plan.state->epoch==epoch&&!profile&&!initial&&!recovery,"NCP4206 scalar/profile lifecycle changed since preflight");}
    void qualify(const Ncp4206Receipt& receipt){require(receipt.state&&receipt.state->session==session&&receipt.state->issued&&receipt.state->plan,"NCP4206 receipt belongs to another driver or has no issued writes");
        require(receipt.state->epoch==epoch&&(!profile||profile==receipt.state)&&!initial,"NCP4206 owned lifecycle was replaced");}
    static bool candidate(const Ncp4206ReceiptState& receipt,const State& current){const auto value=selected(current);return value==selected(receipt.plan->before)||std::find(receipt.candidates.begin(),receipt.candidates.end(),value)!=receipt.candidates.end();}
    void profile_set(const Ncp4206State& target,const Ncp4206Receipt& receipt,const std::function<void(const Ncp4206Receipt&)>& journal={}){
        auto current=profile_capture(false);require(candidate(*receipt.state,current),"NCP4206 selected control changed externally; no write issued");
        const auto dispatch=[&](uint8_t reg,uint8_t value,size_t width){
            const auto before=profile_capture(false);require(selected(before)==selected(current)&&candidate(*receipt.state,before),"NCP4206 selected state changed before dispatch");
            auto next=before;if(reg==0x21)next.command=value;else if(reg==0xd2)next.a=uint8_t((before.a&~8u)|(value&8u));else next.b=uint8_t((before.b&~8u)|(value&8u));
            if(next==before){current=before;return;}
            const auto old_candidates=receipt.state->candidates;const bool first=!receipt.state->issued;
            receipt.state->candidates={selected(before),selected(next)};receipt.state->issued=true;
            if(first)try{if(journal)journal(receipt);}catch(...){receipt.state->issued=false;receipt.state->candidates=old_candidates;throw;}
            profile=receipt.state;recovery=true;++epoch;receipt.state->epoch=epoch;
            const unsigned raw=reg==0x21?next.command:reg==0xd2?next.a:next.b;std::vector<uint8_t> bytes(width);for(size_t i=0;i<width;++i)bytes[i]=uint8_t(raw>>(8*i));
            require(io.write_bytes(I2cFamily::ncp4206,route,reg,bytes),"NCP4206 profile write outcome uncertain; no SET retry sent");
            const auto after=profile_capture(false);require(after==next,"NCP4206 selected or preserved register readback differs; receipt retained");
            current=after;receipt.state->candidates={selected(after)};
        };
        if(target.enabled)dispatch(0x21,target.command,2);
        dispatch(0xd2,target.enabled?8:0,1);dispatch(0xd3,target.enabled?8:0,1);
        if(!target.enabled)dispatch(0x21,target.command,2);
        const auto final=profile_capture();require(selected(final)==selected(target),"NCP4206 final retained command/mode differs");
    }
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
        ControllerView v; v.name="NCP4206"; v.owned=owned(); v.recovery=recovery; v.verified=verified; v.needs_verification=true;
        State s; if(!capture(s)) {v.stale=true; v.message="Controller state unavailable or paired modes disagree"; return v;}
        v.readable=v.capable=true; I2cField f; f.key="voltage"; f.label="Voltage target"; f.unit="mV"; if(s.enabled()) f.value=1600.0-(s.command-2)*6.25; f.minimum=600; f.maximum=xoc?1600:1281; f.step=6.25; f.writable=verified; f.resettable=true; f.reset_label="Auto"; f.detail="VID command; physical rail unassigned"; v.fields.push_back(f); v.message=s.enabled()?"Absolute VID override":"GPU VID mode (Auto)"; return v;
    }
    I2cResult verify(const ControllerContext& context) override {
        verified=false;
        if(!context.gate || recovery || initial || profile) return {false,false,recovery,"Verification requires unlocked controls and no owned or uncertain request"};
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
        if(profile)return {false,false,recovery,"Restore the owned exact NCP4206 profile before scalar edits"};
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
        if(profile)return restore_ncp4206({profile});
        if(!initial) {if(recovery) return recover_auto(); return {true,false,false,"No owned command"};}
        State current; if(!capture_raw(current) || !known(current)) {recovery=true; return {false,false,true,"Controller state unavailable or differs from known dispatch states"};}
        if(!set(*initial)) {recovery=true; last.reset(); return {false,true,true,"Restore failed; command may have changed"};}
        initial.reset(); last.reset(); permitted.clear(); recovery=false; return {true,true,false,"Entry command and mode restored"};
    }
    I2cResult capture_ncp4206(Ncp4206State& out) override {try{out=semantic(profile_capture());return {true,false,recovery,"Exact NCP4206 enabled flag and retained VID command read"};}catch(const std::exception& e){return {false,false,recovery,e.what()};}}
    I2cResult preview_ncp4206(const Ncp4206State& desired,bool xoc,Ncp4206Plan& out) override {
        out.state.reset();try{require(!profile&&!initial&&!recovery,"Release owned scalar/profile NCP4206 control before preflight");const auto before=profile_capture();
            require(valid(desired,xoc),"NCP4206 desired enabled VID exceeds current mode or intrinsic encoding bounds");require(valid(semantic(before),true),"NCP4206 active original has no qualified restoration request");
            require(selected(before)==selected(desired)||verified,"NCP4206 response verification required before changed profile writes");
            auto plan=std::make_shared<Ncp4206PlanState>();plan->session=session;plan->before=semantic(before);plan->desired=desired;plan->xoc=xoc;plan->epoch=epoch;out.state=std::move(plan);return {true,false,false,"Exact NCP4206 command/mode preflighted without writes"};
        }catch(const std::exception& e){return {false,false,recovery,e.what()};}
    }
    I2cResult apply_ncp4206(const Ncp4206Plan& plan,const ControllerContext& context,const std::function<void(const Ncp4206Receipt&)>& journal) override {
        const auto before_epoch=epoch;Ncp4206Receipt receipt;try{qualify(plan);const auto current=profile_capture();require(selected(current)==selected(plan.state->before),"NCP4206 original selected control changed since preflight");
            require(context.xoc==plan.state->xoc&&valid(plan.state->desired,context.xoc),"NCP4206 profile mode or range changed");
            if(selected(current)==selected(plan.state->desired))return {true,false,false,"Exact NCP4206 control already matches; no write or ownership acquired"};
            require(context.gate&&verified,"NCP4206 profile writes require unlocked opt-in and verified response");
            receipt.state=std::make_shared<Ncp4206ReceiptState>();receipt.state->session=session;receipt.state->plan=plan.state;receipt.state->epoch=epoch;receipt.state->candidates={selected(current)};
            profile_set(plan.state->desired,receipt,journal);recovery=false;return {true,true,false,"NCP4206 retained command and paired enabled flag verified; unknown bits preserved"};
        }catch(const std::exception& e){if(receipt.state&&receipt.state->issued){profile=receipt.state;recovery=true;}return {false,epoch!=before_epoch,recovery,e.what()};}
    }
    I2cResult verify_ncp4206(const Ncp4206Plan& plan) override {try{require(plan.state&&plan.state->session==session,"NCP4206 plan belongs to another driver");require((profile&&profile->plan==plan.state&&profile->epoch==epoch)||(!profile&&plan.state->epoch==epoch),"NCP4206 lifecycle changed");require(!recovery&&selected(profile_capture())==selected(plan.state->desired),"NCP4206 selected desired command/mode differs");return {true,false,false,"Exact NCP4206 selected desired control verified"};}catch(const std::exception& e){return {false,false,recovery,e.what()};}}
    I2cResult verify_ncp4206(const Ncp4206Receipt& receipt,bool original=false) override {try{qualify(receipt);require(!recovery&&selected(profile_capture())==selected(original?receipt.state->plan->before:receipt.state->plan->desired),"NCP4206 issued control differs");return {true,false,false,"Exact issued NCP4206 command/mode verified"};}catch(const std::exception& e){return {false,false,recovery,e.what()};}}
    I2cResult restore_ncp4206(const Ncp4206Receipt& receipt) override {const auto before_epoch=epoch;try{qualify(receipt);const auto current=profile_capture(false);require(candidate(*receipt.state,current),"NCP4206 selected control replaced externally; no restoration issued");
            profile_set(receipt.state->plan->before,receipt);receipt.state->candidates={selected(receipt.state->plan->before)};profile.reset();recovery=false;return {true,epoch!=before_epoch,false,"Captured NCP4206 enabled flag and retained command restored; current unknown bits preserved"};
        }catch(const std::exception& e){if(receipt.state&&receipt.state->session==session&&receipt.state->issued&&receipt.state->epoch==epoch&&(!profile||profile==receipt.state)&&!initial){profile=receipt.state;recovery=true;}return {false,epoch!=before_epoch,recovery,e.what()};}}
    bool owned() const override {return bool(initial)||bool(profile);}
};
}
std::unique_ptr<ControllerDriver> make_ncp4206(ControllerIo& io,I2cRoute r) {return std::make_unique<Ncp>(io,r);}
}
