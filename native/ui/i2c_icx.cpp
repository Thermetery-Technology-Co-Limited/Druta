/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "i2c_driver.h"
#include <algorithm>
#include <cmath>
#include <map>
#include <array>
#include <stdexcept>

namespace druta::ui {
struct IcxControllerPlanState {
    std::shared_ptr<int> owner;uint64_t epoch=0;
    std::map<uint8_t,std::vector<uint8_t>> before,target;
    std::vector<uint8_t> identity;
    std::vector<ControllerRequest> requests;
};
struct IcxControllerReceiptState {
    std::shared_ptr<const IcxControllerPlanState> plan;uint64_t epoch=0;
    std::array<bool,7> issued{},completed{};bool gate=false,journaled=false;std::optional<uint8_t> fb_witness;
};
namespace {
struct Spec { const char* key; const char* label; uint8_t state, command, index; int min, max, step; const char* unit; };
constexpr Spec specs[] = {
    {"nvvdd", "NVVDD request", 0x19, 0x10, 1, 750, 1150, 5, "mV"},
    {"fbvdd", "FBVDD request", 0x19, 0x18, 8, 1250, 1450, 5, "mV"},
    {"frequency_nvvdd", "NVVDD switching frequency", 0x1c, 0x13, 1, 300, 1000, 100, "kHz"},
    {"frequency_fbvdd", "FBVDD switching frequency", 0x1c, 0x1f, 2, 300, 1000, 100, "kHz"},
    {"ocp_nvvdd", "NVVDD OCP", 0x1b, 0x12, 1, 0, 1, 1, ""},
    {"ocp_fbvdd", "FBVDD OCP", 0x1b, 0x1e, 2, 0, 1, 1, ""},
    {"loadline", "Loadline", 0x1a, 0x11, 1, 0, 15, 1, ""},
};
const Spec* spec(const std::string& key) { for (const auto& s : specs) if (key == s.key) return &s; return nullptr; }
size_t length(uint8_t r) { return r == 0xb1 ? 6 : r == 0xb2 ? 5 : r == 0x19 ? 10 : 3; }
struct Captured { int initial, last, previous; bool auto_initial, auto_last, auto_previous; };
class Icx final : public ControllerDriver {
    ControllerIo& io_; I2cRoute route_; bool verified_ = false, pending_ = false, gate_touched_ = false;
    std::map<std::string, Captured> owned_;
    std::shared_ptr<int> owner_=std::make_shared<int>(0);uint64_t epoch_=0;
    std::shared_ptr<IcxControllerReceiptState> profile_;
    std::function<void(const Spec*)> before_effect_;
    static void require(bool ok,const char* message){if(!ok)throw std::runtime_error(message);}
    void effect(const Spec* selected){if(before_effect_)before_effect_(selected);++epoch_;if(profile_)profile_->epoch=epoch_;}
    bool block(uint8_t reg, std::vector<uint8_t>& data, bool retry = true) {
        for (int n = 0; n < (retry ? 3 : 1); ++n) {
            if (n) io_.sleep_ms(n == 1 ? 50 : 100);
            data.clear();
            if (!io_.controller_bound()) return false;
            if (io_.read_bytes(route_, reg, length(reg), data))
                return data.size() == length(reg) && data[0] == data.size() - 1 && io_.controller_bound();
        }
        return false;
    }
    bool identity(bool retry = true) {
        std::vector<uint8_t> b;
        return io_.architecture() == uint32_t(7) && route_.port == 1 && route_.addr7 == 0x2d && block(0xb1, b, retry) &&
               b[0] == 5 && b[1] == 0xfc && b[2] == 0x2d && b[3] == 0x13;
    }
    bool decode(const Spec& s, const std::vector<uint8_t>& b, int& v, bool& automatic) {
        automatic = false;
        if (s.state == 0x19) {
            if (s.index == 1) {
                if (b[1] > 1) return false;
                automatic = b[1] == 0;
                v = (int(b[2]) | int(b[3]) << 8) * 5;
                return automatic || v != 0;
            }
            v = (int(b[8]) | int(b[9]) << 8) * 5;
            return v != 0;
        }
        v = b[s.index];
        if (s.state == 0x1c) { if (v < 3 || v > 10) return false; v *= 100; return true; }
        return v <= s.max;
    }
    bool enabled() {
        std::vector<uint8_t> b;
        if (!block(0xb2, b) || (b[1] != 0xfc && b[1] != 0xfe)) return false;
        if (b[1] == 0xfc) return true;
        effect(nullptr);gate_touched_ = true;
        if (!io_.write_bytes(I2cFamily::icx, route_, 0xb2, {4,0xc6,0xeb,0xea,0x15})) return false;
        for (unsigned delay : {0u,100u,300u,600u,500u}) {
            io_.sleep_ms(delay);
            if (block(0xb2,b,false) && b[1] == 0xfc) return true;
        }
        return false;
    }
    I2cResult send(const Spec& s, int target, bool automatic, bool restoring) {
        std::vector<uint8_t> before;
        int previous = 0; bool previous_auto = false;
        if (!identity() || !block(s.state, before) || !decode(s, before, previous, previous_auto))
            return {false,false,pending_,"iCX identity or selected state is unavailable or invalid"};
        auto existing = owned_.find(s.key);
        if (existing == owned_.end() && s.state == 0x19 && !previous_auto &&
            (previous < s.min || previous > (s.index == 1 ? 1600 : s.max) || previous % 5))
            return {false,false,pending_,"Captured iCX request cannot be restored through the qualified packet contract; no write sent"};
        if (!restoring && existing != owned_.end() &&
            !(previous_auto == existing->second.auto_last && (previous_auto || previous == existing->second.last)))
            return {false,false,true,"iCX selected state changed outside this session; new request refused"};
        if (restoring && (existing == owned_.end() ||
            !((previous_auto == existing->second.auto_last && (previous_auto || previous == existing->second.last)) ||
              (previous_auto == existing->second.auto_previous && (previous_auto || previous == existing->second.previous)) ||
              (previous_auto == automatic && (automatic || previous == target)))))
            return {false,false,true,"iCX selected state changed outside this session; restoration refused"};
        if (previous_auto == automatic && (automatic || previous == target)) return {true,false,pending_,"iCX request already matches"};
        std::map<uint8_t,std::vector<uint8_t>> collateral;
        if (s.state != 0x19) for (uint8_t reg : {uint8_t(0x19),uint8_t(0x1a),uint8_t(0x1b),uint8_t(0x1c)}) {
            std::vector<uint8_t> b; if (reg != s.state && block(reg,b)) collateral[reg] = b;
        }
        if (!enabled()) return {false,gate_touched_,pending_,"iCX gate enable unavailable or unverified; no setting request sent. Gate may remain enabled."};
        effect(&s);
        if (existing == owned_.end()) {
            owned_[s.key] = {previous,target,previous,previous_auto,automatic,previous_auto};
        } else { existing->second.previous = previous; existing->second.auto_previous = previous_auto; existing->second.last = target; existing->second.auto_last = automatic; }
        pending_ = true;
        std::vector<uint8_t> payload;
        if (s.state == 0x19) {
            int count = target / 5;
            payload = {3, uint8_t(automatic ? 0 : 1), uint8_t(automatic ? 0 : count & 255), uint8_t(automatic ? 0 : count >> 8)};
        } else payload = {1, uint8_t(s.state == 0x1c ? target / 100 : target)};
        if (!io_.write_bytes(I2cFamily::icx, route_, s.command, payload))
            return {false,true,true,"iCX write outcome uncertain; no retry sent. Restoration remains available."};
        bool matched = false;
        for (unsigned delay : {0u,100u,300u,600u,500u}) {
            io_.sleep_ms(delay); std::vector<uint8_t> after;
            if (!block(s.state,after,false)) continue;
            bool preserved = true;
            for (size_t i = 0; i < before.size(); ++i) {
                bool selected = s.state == 0x19 ? (s.index == 1 ? i >= 1 && i <= 3 : i >= 7 && i <= 9) : i == s.index;
                if (!selected && before[i] != after[i]) preserved = false;
            }
            int value = 0; bool is_auto = false;
            if (!preserved) return {false,true,true,"iCX unrelated or unknown state changed; no further write sent"};
            if (decode(s,after,value,is_auto) && is_auto == automatic && (automatic || value == target)) { matched = true; break; }
        }
        if (!matched) return {false,true,true,"iCX request readback was not verified; no further write sent"};
        for (const auto& [reg, old] : collateral) { std::vector<uint8_t> now; if (block(reg,now) && now != old)
            return {false,true,true,"iCX unrelated setting changed; no further write sent"}; }
        owned_[s.key].previous = target; owned_[s.key].auto_previous = automatic;
        pending_ = false;
        return {true,true,false,"iCX controller request verified; physical voltage is not measured. B2 may remain enabled."};
    }
    using Packets=std::map<uint8_t,std::vector<uint8_t>>;
    static size_t index(const Spec& s){return static_cast<size_t>(&s-specs);}
    static bool selected(const Spec& s,size_t i){return s.state==0x19?(s.index==1?i>=1&&i<=3:i>=7&&i<=9):i==s.index;}
    // Auto has no retained manual request in the released semantic contract.
    // Bytes 2/3 may retain or report a value; only mode byte 1 defines Auto.
    static bool same_packet(uint8_t reg,const std::vector<uint8_t>& x,const std::vector<uint8_t>& y){if(x.size()!=y.size())return false;for(size_t i=0;i<x.size();++i){if(reg==0x19&&x[1]==0&&y[1]==0&&(i==2||i==3))continue;if(x[i]!=y[i])return false;}return true;}
    static bool same_packets(const Packets& a,const Packets& b){if(a.size()!=b.size())return false;for(const auto& [reg,x]:a){const auto found=b.find(reg);if(found==b.end()||!same_packet(reg,x,found->second))return false;}return true;}
    static bool slice(const Spec& s,const Packets& a,const Packets& b,bool issued=false){const auto& x=a.at(s.state);const auto& y=b.at(s.state);if(x.size()!=y.size())return false;for(size_t i=0;i<x.size();++i){if(s.state==0x19&&s.index==1&&x[1]==0&&y[1]==0&&(i==2||i==3))continue;if(selected(s,i)&&!(issued&&s.state==0x19&&s.index==8&&i==7)&&x[i]!=y[i])return false;}return true;}
    static void project(const Spec& s,const ControllerRequest& r,Packets& state){auto& b=state.at(s.state);if(s.state==0x19){const size_t first=s.index==1?1u:7u;const int count=r.reset?0:int(r.value)/5;if(first==1)b[first]=r.reset?0:1;b[first+1]=uint8_t(count);b[first+2]=uint8_t(count>>8);}else b[s.index]=uint8_t(s.state==0x1c?r.value/100:r.value);}
    static bool valid_request(const Spec& s,const ControllerRequest& r,bool xoc){if(r.reset)return r.key=="nvvdd";const int maximum=r.key=="nvvdd"&&xoc?1600:s.max;return std::isfinite(r.value)&&r.value>=s.min&&r.value<=maximum&&std::trunc(r.value)==r.value&&int(r.value)%s.step==0;}
    Packets capture(std::vector<uint8_t>* firmware=nullptr){require(identity(),"iCX identity unavailable or changed");std::vector<uint8_t> id;require(block(0xb1,id),"iCX firmware unavailable");Packets state;
        for(uint8_t reg:{uint8_t(0x19),uint8_t(0x1a),uint8_t(0x1b),uint8_t(0x1c)}){std::vector<uint8_t> a,b;require(block(reg,a)&&block(reg,b)&&same_packet(reg,a,b),"Stable complete iCX packet state unavailable");state[reg]=std::move(b);}
        for(const auto& s:specs){int value=0;bool automatic=false;require(decode(s,state.at(s.state),value,automatic),"iCX field decode unavailable");ControllerRequest r{s.key,double(value),automatic};require(valid_request(s,r,true),"Captured iCX request has no qualified restoration");}
        std::vector<uint8_t> after;require(identity()&&block(0xb1,after)&&id==after,"iCX firmware changed during capture");if(firmware)*firmware=std::move(id);return state;
    }
    static std::vector<ControllerRequest> ordered(const std::vector<ControllerRequest>& requests){std::vector<ControllerRequest> out;const auto add=[&](const char* key){for(const auto& r:requests)if(r.key==key)out.push_back(r);};
        for(const auto& r:requests)if(r.key=="nvvdd"&&r.reset)out.push_back(r);
        for(const char* key:{"ocp_nvvdd","ocp_fbvdd"})for(const auto& r:requests)if(r.key==key&&r.value==1)out.push_back(r);
        for(const char* key:{"fbvdd","frequency_nvvdd","frequency_fbvdd","loadline"})add(key);
        for(const auto& r:requests)if(r.key=="nvvdd"&&!r.reset)out.push_back(r);
        for(const char* key:{"ocp_nvvdd","ocp_fbvdd"})for(const auto& r:requests)if(r.key==key&&r.value==0)out.push_back(r);return out;
    }
    std::vector<ControllerRequest> originals(const IcxControllerPlanState& plan){std::vector<ControllerRequest> out;for(const auto& s:specs){int value=0;bool automatic=false;require(decode(s,plan.before.at(s.state),value,automatic),"Original iCX decode failed");out.push_back({s.key,double(value),automatic});}return out;}
    Packets canonical_original(const IcxControllerPlanState& plan){auto state=plan.before;for(const auto& r:originals(plan))project(*spec(r.key),r,state);return state;}
    void binding(const IcxControllerPlanState& plan){require(plan.owner==owner_,"iCX transaction belongs to another driver/session");std::vector<uint8_t> id;require(identity()&&block(0xb1,id)&&id==plan.identity,"iCX selected firmware/binding changed");}
    void candidates(const IcxControllerReceiptState& receipt,const Packets& current){const auto& plan=*receipt.plan;binding(plan);const auto canonical=canonical_original(plan);
        for(const auto& [reg,b]:current)for(size_t i=0;i<b.size();++i){bool known=false;for(const auto& s:specs)known=known||(s.state==reg&&selected(s,i));if(!known)require(b[i]==plan.before.at(reg)[i],"iCX unrelated/unknown packet byte changed");}
        for(const auto& s:specs){const auto n=index(s);const bool before=slice(s,current,plan.before,receipt.issued[n]),original=slice(s,current,canonical,receipt.issued[n]);if(!receipt.issued[n])require(before,"Unissued iCX field changed externally");else if(receipt.completed[n])require(before||original,"Completed iCX restoration changed externally");else require(before||original||slice(s,current,plan.target,true),"Issued iCX field changed outside its candidates");}
        if(receipt.issued[1]&&receipt.fb_witness)require(current.at(0x19)[7]==*receipt.fb_witness,"Observed iCX FBVDD byte 7 changed outside an issued operation");
    }
    std::shared_ptr<IcxControllerReceiptState> receipt(const IcxControllerReceipt& r){require(r.state&&r.state->plan&&r.state->journaled,"Missing/unissued iCX receipt");binding(*r.state->plan);require(r.state->epoch==epoch_&&(!profile_||profile_==r.state)&&(profile_||owned_.empty()),"iCX ownership lifecycle replaced");return r.state;}
public:
    Icx(ControllerIo& io, I2cRoute route) : io_(io), route_(route) {}
    bool probe() override { return identity(false); }
    ControllerView read(bool xoc) override {
        ControllerView v; v.name = "EVGA iCX"; v.owned = owned(); v.recovery = pending_; v.verified = verified_;
        v.needs_verification = false;
        if (!identity()) { v.stale = true; v.message = "iCX identity unavailable or changed"; return v; }
        v.readable = v.capable = true; v.message = "Controller requests, not physical voltage; B2 enable has no known disable command.";
        std::map<uint8_t,std::vector<uint8_t>> groups;
        for (uint8_t r : {uint8_t(0x19),uint8_t(0x1a),uint8_t(0x1b),uint8_t(0x1c)}) { std::vector<uint8_t> b; if (block(r,b)) groups[r] = b; }
        for (const auto& s : specs) {
            auto it = groups.find(s.state); int value = 0; bool automatic = false;
            if (it == groups.end() || !decode(s,it->second,value,automatic)) continue;
            I2cField f; f.key=s.key; f.label=s.label; f.unit=s.unit; f.value = automatic ? std::optional<double>{} : std::optional<double>{double(value)};
            f.minimum=s.min; f.maximum=(s.key == std::string("nvvdd") && xoc) ? 1600 : s.max; f.step=s.step; f.writable=true;
            if (s.key == std::string("nvvdd")) { f.resettable=true; f.reset_label="Auto"; if (automatic) f.detail="Auto"; }
            if (s.state == 0x1b) f.choices={{"Disabled",0},{"Enabled",1}};
            v.fields.push_back(f);
        }
        return v;
    }
    I2cResult verify(const ControllerContext&) override {
        if (!identity()) return {false,false,pending_,"iCX identity unavailable or invalid"};
        auto view = read(false); if (view.fields.empty()) return {false,false,pending_,"iCX setting states are unavailable or not understood"};
        verified_ = true; return {true,false,pending_,"iCX identity and readable settings verified read-only; physical voltage requires a meter"};
    }
    I2cResult apply(const ControllerRequest& request, const ControllerContext& context) override {
        if(profile_)return {false,false,pending_,"Restore the active iCX profile before scalar mutation"};
        const auto* s = spec(request.key);
        if (!s || !context.gate) return {false,false,pending_,"iCX request requires a known field and acknowledged gate"};
        bool automatic = request.reset && request.key == "nvvdd";
        if (request.reset && !automatic) return {false,false,pending_,"No reset is defined for this iCX setting"};
        double value = request.value; int maximum = request.key == "nvvdd" && context.xoc ? 1600 : s->max;
        if (!automatic && (!std::isfinite(value) || value < s->min || value > maximum || std::floor(value) != value || int(value) % s->step))
            return {false,false,pending_,"iCX requested value is outside the understood range or step"};
        return send(*s, automatic ? 0 : int(value), automatic, false);
    }
    I2cResult restore() override {
        if(profile_)return restore_icx({profile_});
        for (auto it = owned_.begin(); it != owned_.end();) {
            const auto* s = spec(it->first); auto captured = it->second;
            auto result = send(*s,captured.initial,captured.auto_initial,true);
            if (!result.ok) { pending_=true; result.recovery_pending=true; return result; }
            it = owned_.erase(it);
        }
        pending_=false; return {true,gate_touched_,false,"Captured iCX requests restored. B2 may remain enabled; no disable command is known."};
    }
    I2cResult preview_icx(const std::vector<ControllerRequest>& desired,const ControllerContext& context,IcxControllerPlan& out) override {
        out={};try{require(!owned()&&context.gate,"Release existing iCX ownership and acknowledge controls before preflight");require(desired.size()==7,"A complete iCX preset requires seven unique controls");
            std::map<std::string,bool> seen;for(const auto& r:desired){const auto* s=spec(r.key);require(s&&seen.emplace(r.key,true).second&&valid_request(*s,r,context.xoc),"Unknown, duplicate or invalid iCX preset control");}
            auto plan=std::make_shared<IcxControllerPlanState>();plan->owner=owner_;plan->epoch=epoch_;plan->before=capture(&plan->identity);plan->target=plan->before;plan->requests=ordered(desired);
            for(const auto& r:plan->requests){const auto& s=*spec(r.key);int value=0;bool automatic=false;require(decode(s,plan->before.at(s.state),value,automatic),"Original iCX decode failed");if(automatic!=r.reset||(!automatic&&value!=r.value))project(s,r,plan->target);}
            out.state=std::move(plan);return {true,false,false,"Complete iCX prior and desired requests preflighted without effects"};
        }catch(const std::exception& e){return {false,false,pending_,e.what()};}
    }
    I2cResult apply_icx(const IcxControllerPlan& wrapped,const ControllerContext& context,const std::function<void(const IcxControllerReceipt&)>& journal) override {
        const auto start=epoch_;std::shared_ptr<IcxControllerReceiptState> saved;
        try{require(wrapped.state&&wrapped.state->epoch==epoch_&&!owned(),"iCX plan lifecycle changed");const auto& plan=*wrapped.state;binding(plan);require(context.gate,"Acknowledge iCX controls before Apply");for(const auto& r:plan.requests)require(valid_request(*spec(r.key),r,context.xoc),"iCX requested value exceeds current mode");
            require(same_packets(capture(),plan.before),"iCX original state changed since preflight");if(same_packets(plan.target,plan.before))return {true,false,false,"All iCX requests already match; no ownership acquired"};
            saved=std::make_shared<IcxControllerReceiptState>();saved->plan=wrapped.state;saved->epoch=epoch_;saved->fb_witness=plan.before.at(0x19)[7];
            before_effect_=[&,saved](const Spec* s){candidates(*saved,capture());if(!saved->journaled){try{if(journal)journal({saved});}catch(...){saved->journaled=false;throw;}saved->journaled=true;profile_=saved;}
                if(s){saved->issued[index(*s)]=true;if(s->state==0x19&&s->index==8)saved->fb_witness.reset();}else saved->gate=true;};
            struct Clear {std::function<void(const Spec*)>& fn;~Clear(){fn={};}}clear{before_effect_};
            for(const auto& r:plan.requests){const auto& s=*spec(r.key);auto current=capture();candidates(*saved,current);if(slice(s,current,plan.target))continue;
                const auto step=send(s,r.reset?0:int(r.value),r.reset,false);if(saved->issued[1]&&!saved->fb_witness){auto observed=capture();saved->fb_witness=observed.at(0x19)[7];}
                require(step.ok,step.message.c_str());candidates(*saved,capture());}
            const auto current=capture();candidates(*saved,current);for(const auto& s:specs)require(slice(s,current,plan.target,saved->issued[index(s)]),"Complete iCX desired readback differs");
            pending_=false;return {true,epoch_!=start,false,"Complete iCX requests verified; B2 may remain enabled"};
        }catch(const std::exception& e){before_effect_={};if(saved&&saved->journaled){profile_=saved;pending_=true;}return {false,epoch_!=start,pending_,e.what()};}
    }
    I2cResult verify_icx(const IcxControllerPlan& wrapped) override {try{require(bool(wrapped.state),"Missing iCX plan");const auto& plan=*wrapped.state;binding(plan);require((profile_&&profile_->plan==wrapped.state&&profile_->epoch==epoch_)||(!profile_&&plan.epoch==epoch_),"iCX plan lifecycle replaced");require(!pending_,"iCX request outcome uncertain");const auto current=capture();
            if(profile_)candidates(*profile_,current);else require(same_packets(current,plan.target),"Complete matching iCX request or unknown state changed");
            for(const auto& s:specs)require(slice(s,current,plan.target,profile_&&profile_->issued[index(s)]),"Complete desired iCX field differs");return {true,false,false,"Complete iCX desired requests verified"};
        }catch(const std::exception& e){return {false,false,pending_,e.what()};}}
    I2cResult verify_icx(const IcxControllerReceipt& wrapped,bool original=false) override {try{const auto saved=receipt(wrapped);require(!pending_,"iCX issued state remains uncertain");const auto current=capture();candidates(*saved,current);const auto expected=original?canonical_original(*saved->plan):saved->plan->target;
            for(const auto& s:specs){const auto n=index(s);require(slice(s,current,expected,saved->issued[n])||(original&&slice(s,current,saved->plan->before,saved->issued[n])),"iCX issued original/desired field differs");}return {true,false,false,"Complete iCX receipt requests verified"};
        }catch(const std::exception& e){return {false,false,pending_,e.what()};}}
    I2cResult restore_icx(const IcxControllerReceipt& wrapped) override {
        const auto start=epoch_;std::shared_ptr<IcxControllerReceiptState> saved;
        try{saved=receipt(wrapped);auto current=capture();candidates(*saved,current);profile_=saved;
            before_effect_=[this,saved](const Spec* s){candidates(*saved,capture());if(s&&s->state==0x19&&s->index==8)saved->fb_witness.reset();};
            struct Clear {std::function<void(const Spec*)>& fn;~Clear(){fn={};}}clear{before_effect_};
            const auto canonical=canonical_original(*saved->plan);
            for(const auto& r:ordered(originals(*saved->plan))){const auto& s=*spec(r.key);const auto n=index(s);if(!saved->issued[n]||saved->completed[n])continue;
                current=capture();candidates(*saved,current);if(!slice(s,current,saved->plan->before,true)&&!slice(s,current,canonical,true)){
                    const auto step=send(s,r.reset?0:int(r.value),r.reset,true);if(saved->issued[1]&&!saved->fb_witness){auto observed=capture();saved->fb_witness=observed.at(0x19)[7];}require(step.ok,step.message.c_str());}
                current=capture();candidates(*saved,current);require(slice(s,current,saved->plan->before,true)||slice(s,current,canonical,true),"Restored iCX field differs");saved->completed[n]=true;owned_.erase(s.key);
            }
            current=capture();candidates(*saved,current);pending_=false;profile_.reset();return {true,epoch_!=start,false,"Issued iCX requests restored; B2 may remain enabled"};
        }catch(const std::exception& e){before_effect_={};if(saved){profile_=saved;pending_=true;}return {false,epoch_!=start,pending_,e.what()};}
    }
    bool owned() const override { return !owned_.empty() || pending_ || bool(profile_); }
    bool needs_clock_clamp() const override { return true; }
};
}
std::unique_ptr<ControllerDriver> make_icx(ControllerIo& io, I2cRoute route) { return std::make_unique<Icx>(io,route); }
}
