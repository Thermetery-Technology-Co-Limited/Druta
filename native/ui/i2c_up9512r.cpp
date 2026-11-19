/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "i2c_driver.h"
#include <algorithm>
#include <array>
#include <cmath>
#include <set>
#include <stdexcept>
namespace druta::ui {
struct Up9512rPlanState {
    std::shared_ptr<int> session;
    std::array<uint8_t,4> before{},target{};
    Up9512rControl desired;
    uint64_t epoch=0;
};
struct Up9512rReceiptState {
    std::shared_ptr<int> session;
    std::shared_ptr<const Up9512rPlanState> plan;
    std::array<uint8_t,4> before{},target{};
    std::set<std::array<uint8_t,4>> candidates;
    uint8_t issued=0;
    uint64_t epoch=0;
};
namespace {
using Raw = std::array<uint8_t,4>;
constexpr uint8_t regs[] = {0x0a,0x0b,0x0c,0x2a};
constexpr uint8_t masks[] = {0xff,0xff,0xf0,0x40};
class Up final : public ControllerDriver {
    ControllerIo& io_; I2cRoute route_; bool verified_=false, owned_=false, pending_=false;
    Raw initial_{}; std::set<Raw> accepted_;
    std::shared_ptr<int> session_=std::make_shared<int>(0);
    std::shared_ptr<Up9512rReceiptState> profile_;
    uint64_t epoch_=0;
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
    static void require(bool condition,const char* message) { if(!condition)throw std::runtime_error(message); }
    Raw profile_capture() { Raw raw;require(capture(raw),"Stable uP9512R identity/control readback unavailable");return raw; }
    static Up9512rControl decode_control(const Raw& raw) { Up9512rControl result;const auto values=offsets(raw);for(size_t i=0;i<5;++i)result.offsets_mv[i]=values[i];result.enabled=(raw[3]&0x40)!=0;return result; }
    static Raw encode_control(const Raw& raw,const Up9512rControl& desired) { std::array<int,5> values{};for(size_t i=0;i<5;++i)values[i]=desired.offsets_mv[i];return encode(raw,values,desired.enabled); }
    static void valid_control(const Up9512rControl& desired,bool xoc) { for(auto value:desired.offsets_mv){require(value>=0&&value<=150&&value%10==0,"uP9512R offsets require exact 0..150 mV values in 10 mV steps");require(!desired.enabled||xoc||value<=50,"Enabled uP9512R offsets exceed the normal 50 mV software guard");} }
    void profile_guard(const Up9512rControl& desired,const Raw& before,const ControllerContext& context) {
        valid_control(desired,context.xoc);require(!(before[3]&0x83),"uP9512R internal-test bits select an unknown control layout");
        if(encode_control(before,desired)==before)return; // Released matching-control exemption is entirely read-only.
        require(context.gate&&verified_,"Enable I2C and verify the selected controller before changed profile requests");
        require(writable(before),"uP9512R lock is unreadable, locked or layout unknown; no write issued");
        int mv=0;const int ceiling=context.xoc?2000:1200;require(voltage(mv)&&mv>=300&&mv<=ceiling,"uP9512R FB ADC unavailable or outside software guard");
        const auto old=offsets(before);int increase=0;for(size_t i=0;i<5;++i)increase=std::max(increase,(desired.enabled?desired.offsets_mv[i]:0)-((before[3]&0x40)?old[i]:0));
        require(mv+increase<=ceiling,"uP9512R largest possible load-state increase exceeds the FB ADC software ceiling");
    }
    void qualify(const Up9512rPlan& plan) { require(plan.state&&plan.state->session==session_,"uP9512R plan belongs to another selected driver");require(plan.state->epoch==epoch_&&!owned()&&!pending_,"uP9512R ownership changed since profile preflight"); }
    void qualify(const Up9512rReceipt& receipt) { require(receipt.state&&receipt.state->session==session_&&receipt.state->issued,"uP9512R receipt belongs to another driver or has no issued fields");require(receipt.state->epoch==epoch_&&(!profile_||profile_==receipt.state),"uP9512R owned lifecycle was replaced"); }
    static bool selected_raw(const Raw& a,const Raw& b,uint8_t issued) { for(size_t i=0;i<4;++i)if((issued&(1u<<i))&&(a[i]&masks[i])!=(b[i]&masks[i]))return false;return true; }
    void candidate(const Up9512rReceiptState& receipt,const Raw& current) { require(same_unknown(current,receipt.before),"uP9512R preserved bits changed; restoration refused");const auto selection=uint8_t(receipt.issued|8u);require(selected_raw(current,receipt.before,selection)||std::any_of(receipt.candidates.begin(),receipt.candidates.end(),[&](const auto& known){return selected_raw(current,known,selection);}),"Issued uP9512R fields or enable state changed outside this transaction; restoration refused"); }
    bool dispatch(Raw current,const Raw& target) {
        const int normal[]={0,1,2,3}, disable[]={3,0,1,2}; const int* order=(target[3]&0x40)?normal:disable;
        for(int n=0;n<4;++n) { int i=order[n]; if(current[i]==target[i]) continue; Raw check;
            if(!capture(check)||check!=current||!writable(current)) return false;
            Raw next=current; next[i]=target[i]; accepted_.insert(current); accepted_.insert(next); pending_=true;
            ++epoch_;if(!io_.write_bytes(I2cFamily::up9512r,route_,regs[i],{target[i]})) return false;
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
        ControllerView v; v.name="uP9512R offsets"; v.verified=verified_; v.owned=owned(); v.recovery=pending_;
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
        if(profile_)return {false,false,pending_,"Restore the owned compound uP9512R profile before scalar edits"};
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
    I2cResult restore() override { if(profile_)return restore_up9512r({profile_});if(!owned_) return {true,false,false,"No owned offset request"}; bool ok=restore_entry(); if(ok) owned_=false; return {ok,true,!ok,ok?"Exact captured offset state restored":"Offset restoration pending; state unavailable or changed"}; }
    I2cResult verify(const ControllerContext& ctx) override {
        if(!ctx.gate||owned()||!point()) return {false,false,pending_,"Verification requires acknowledgment, a held P0 operating point, and no owned request"};
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
    bool owned() const override { return owned_||(profile_&&profile_->issued); }
    I2cResult capture_up9512r(Up9512rControl& out) override { try{out=decode_control(profile_capture());return {true,false,pending_,"Five exact uP9512R offsets and enable state read"};}catch(const std::exception& error){return {false,false,pending_,error.what()};} }
    I2cResult preview_up9512r(const Up9512rControl& desired,const ControllerContext& context,Up9512rPlan& out) override {
        out={};try{require(!owned()&&!pending_,"Release existing uP9512R requests before independent profile preflight");const auto before=profile_capture();profile_guard(desired,before,context);auto plan=std::make_shared<Up9512rPlanState>();plan->session=session_;plan->before=before;plan->desired=desired;plan->target=encode_control(before,desired);plan->epoch=epoch_;out.state=std::move(plan);return {true,false,false,"Compound offsets and enable preflighted without writes"};}catch(const std::exception& error){return {false,false,pending_,error.what()};}
    }
    I2cResult apply_up9512r(const Up9512rPlan& plan,const ControllerContext& context,const std::function<void(const Up9512rReceipt&)>& journal) override {
        I2cResult result;Up9512rReceipt receipt;
        try{qualify(plan);const auto& frozen=*plan.state;auto current=profile_capture();require(current==frozen.before,"uP9512R full original state changed since profile preflight");profile_guard(frozen.desired,current,context);
            if(current==frozen.target)return {true,false,false,"Compound uP9512R control already matches; no write or ownership acquired"};
            receipt.state=std::make_shared<Up9512rReceiptState>();auto& saved=*receipt.state;saved.session=session_;saved.plan=plan.state;saved.before=frozen.before;saved.target=frozen.target;saved.epoch=epoch_;saved.candidates.insert(current);
            const int normal[]={0,1,2,3},disable[]={3,0,1,2};const int* order=frozen.desired.enabled?normal:disable;
            for(int n=0;n<4;++n){const auto i=static_cast<size_t>(order[n]);if(current[i]==frozen.target[i])continue;require(profile_capture()==current&&writable(current),"uP9512R control or lock changed before profile dispatch");Raw next=current;next[i]=frozen.target[i];const bool first=!saved.issued;saved.issued|=uint8_t(1u<<i);saved.candidates={current,next};
                if(first){try{if(journal)journal(receipt);}catch(...){saved.issued=0;throw;}}profile_=receipt.state;++epoch_;saved.epoch=epoch_;pending_=true;result.may_have_changed=true;
                const bool ok=io_.write_bytes(I2cFamily::up9512r,route_,regs[i],{next[i]});const auto after=profile_capture();require(ok&&after==next,"uP9512R selected/preserved-byte readback unverified; issued originals retained");current=next;saved.candidates={current};
            }
            int mv=0;const int ceiling=context.xoc?2000:1200;require(writable(current)&&voltage(mv)&&mv>=300&&mv<=ceiling&&profile_capture()==frozen.target,"uP9512R post-write lock/FB/control verification failed; issued originals retained");pending_=false;result.ok=true;result.message="Five independent offsets and enable read back exactly; unknown bits preserved";
        }catch(const std::exception& error){result.message=error.what();}result.recovery_pending=pending_;return result;
    }
    I2cResult verify_up9512r(const Up9512rPlan& plan) override {try{require(plan.state&&plan.state->session==session_,"uP9512R plan belongs to another driver");require((profile_&&profile_->plan==plan.state&&profile_->epoch==epoch_)||(!profile_&&plan.state->epoch==epoch_),"uP9512R profile lifecycle changed");const auto current=profile_capture();require(!pending_&&!(current[3]&0x83)&&same_unknown(current,plan.state->before)&&current==plan.state->target,"Selected uP9512R compound control differs or is unconfirmed");return {true,false,false,"Five selected offsets and enable verified"};}catch(const std::exception& error){return {false,false,pending_,error.what()};}}
    I2cResult verify_up9512r(const Up9512rReceipt& receipt,bool original=false) override {try{qualify(receipt);const auto current=profile_capture();require(!pending_&&same_unknown(current,receipt.state->before)&&selected_raw(current,original?receipt.state->before:receipt.state->target,receipt.state->issued),"Issued uP9512R fields differ or are unconfirmed");return {true,false,false,"Issued uP9512R selected fields verified"};}catch(const std::exception& error){return {false,false,pending_,error.what()};}}
    I2cResult restore_up9512r(const Up9512rReceipt& receipt) override {
        I2cResult result;try{qualify(receipt);auto& saved=*receipt.state;auto current=profile_capture();candidate(saved,current);Raw target=current;for(size_t i=0;i<4;++i)if(saved.issued&(1u<<i))target[i]=uint8_t((current[i]&~masks[i])|(saved.before[i]&masks[i]));
            if(!(current[3]&0x40)&&(target[3]&0x40))require(offsets(target)==offsets(saved.before),"uP9512R Undo cannot enable externally replaced unissued offsets");
            if(current!=target)require(writable(current),"uP9512R restoration lock is unreadable, locked or layout unknown");const int normal[]={0,1,2,3},disable[]={3,0,1,2};const int* order=(target[3]&0x40)?normal:disable;
            for(int n=0;n<4;++n){const auto i=static_cast<size_t>(order[n]);if(current[i]==target[i])continue;const auto fresh=profile_capture();candidate(saved,fresh);require(fresh==current&&writable(fresh),"uP9512R control or lock changed during restoration");Raw next=current;next[i]=target[i];saved.candidates={current,next};profile_=receipt.state;++epoch_;saved.epoch=epoch_;pending_=true;result.may_have_changed=true;
                const bool ok=io_.write_bytes(I2cFamily::up9512r,route_,regs[i],{next[i]});const auto after=profile_capture();require(ok&&after==next,"uP9512R restoration write/readback unverified; partial recovery retained");current=next;saved.candidates={current};
            }
            const auto after=profile_capture();candidate(saved,after);require(same_unknown(after,saved.before)&&selected_raw(after,saved.before,saved.issued),"Final issued uP9512R original fields differ");if(profile_==receipt.state)profile_.reset();pending_=false;result.ok=true;result.message="Issued original offsets/enable restored; unissued fields and unknown bits preserved";
        }catch(const std::exception& error){result.message=error.what();}result.recovery_pending=pending_;return result;
    }
};
}
std::unique_ptr<ControllerDriver> make_up9512r(ControllerIo& io,I2cRoute r) { return std::make_unique<Up>(io,r); }
}
