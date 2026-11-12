/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "i2c_controls.h"
#include <algorithm>
#include <cmath>
#include <limits>

namespace druta::ui {
namespace {
bool same_route(I2cRoute a, I2cRoute b) { return a.port == b.port && a.addr7 == b.addr7; }
bool same(const Mp2888Control& a, const Mp2888Control& b) {
    return same_route(a.route,b.route) && a.page==b.page && a.address==b.address && a.command==b.command &&
        a.mode==b.mode && a.operation==b.operation && a.pwm==b.pwm && a.offset==b.offset &&
        a.sense==b.sense && a.fine==b.fine && a.minimum==b.minimum && a.maximum==b.maximum;
}
bool context(const Mp2888Control& a, const Mp2888Control& b) {
    auto x = a; x.command = b.command; x.mode = (x.mode & ~0x10u) | (b.mode & 0x10u);
    return same(x,b);
}
int signed_bits(uint32_t value, unsigned width) {
    const uint32_t mask = (1u << width) - 1; value &= mask;
    return value & (1u << (width-1)) ? static_cast<int>(value) - static_cast<int>(1u << width) : static_cast<int>(value);
}
bool valid_route(I2cRoute r) { return r.port < 8 && r.addr7 >= 0x08 && r.addr7 <= 0x77; }
bool shape(const Mp2888Control& s) {
    return valid_route(s.route) && s.page == 0 && (s.address & 0x7f) == s.route.addr7 &&
        s.address <= 255 && s.command <= 0x1ff && s.mode <= 0xffff && s.operation <= 255 &&
        s.pwm <= 0xffff && s.offset <= 0xff && signed_bits(s.offset,8) >= -111 && signed_bits(s.offset,8) <= 112 &&
        s.sense <= 0xffff && s.fine <= 255 && s.minimum <= s.maximum && s.maximum <= 0x1ff;
}
bool bounds(const Mp2888Control& s, int64_t& low, int64_t& high) {
    if (!shape(s) || (s.sense & 0xc000) || ((s.sense >> 9) & 3) == 3 ||
        (s.sense & 0x1ff) != 32 || (s.sense & 0x800) || (s.fine & 0x80) || signed_bits(s.fine,7) == -64) return false;
    const int64_t fine = std::abs(signed_bits(s.fine,7)) * int64_t{500};
    const int64_t offset = signed_bits(s.offset,8) * int64_t{6250};
    const int64_t ceiling = std::min<int64_t>(static_cast<int64_t>(s.maximum) * 6250, 1200000);
    low = std::max<int64_t>(0, static_cast<int64_t>(s.minimum) * 6250 - offset + fine);
    high = std::min<int64_t>(3193750, ceiling - offset - fine);
    if (high < 0) return false;
    low = ((low + 6249) / 6250) * 6250; high = (high / 6250) * 6250;
    return low <= high;
}
I2cResult error(const char* why, bool changed = false) { I2cResult r; r.message = why; r.may_have_changed = changed; r.recovery_pending = changed; return r; }
}
I2cControls::I2cControls(I2cTransport& transport) : transport_(transport) {}
std::vector<double> I2cControls::nominal_presets(double low_mv, double high_mv) {
    if (!std::isfinite(low_mv) || !std::isfinite(high_mv) || low_mv < 0 || high_mv > 511 * nominal_step_mv || low_mv > high_mv) return {};
    const int first = static_cast<int>(std::ceil(low_mv / nominal_step_mv));
    const int last = static_cast<int>(std::floor(high_mv / nominal_step_mv));
    if (first > last) return {};
    std::vector<double> values{first * nominal_step_mv};
    // Sixteen command bins give readable 100 mV intervals without implying
    // a measured response. Include both live-range endpoints as well.
    for (int bin = ((first / 16) + 1) * 16; bin < last; bin += 16) values.push_back(bin * nominal_step_mv);
    if (last != first) values.push_back(last * nominal_step_mv);
    return values;
}
bool I2cControls::read(uint8_t reg, uint8_t width, uint32_t& value) {
    if (!route_ || !transport_.bound()) return false;
    for (int i=0;i<3;++i) {
        uint32_t data=0;
        try {
            if (transport_.read(route_->port,route_->addr7,reg,width,data) && transport_.bound() &&
                data < (width == 1 ? 256u : 65536u)) { value=data; return true; }
        } catch (...) { /* bounded read-only retry */ }
    }
    return false;
}
bool I2cControls::fingerprint(I2cRoute route, double& vout) {
    if (!valid_route(route) || !transport_.bound()) return false;
    auto old=route_; route_=route;
    struct Sample { uint32_t addr,offset,limit,vout,current,temp,config; } a{}, b{};
    auto sample=[&](Sample& s) {
        return read(0xbe,1,s.addr) && (s.addr & 0x7f)==route.addr7 && read(0x23,2,s.offset) &&
            read(0x24,2,s.limit) && read(0x8b,2,s.vout) && read(0x8c,2,s.current) &&
            read(0x8d,2,s.temp) && read(0x44,2,s.config) && !(s.offset & 0xff00) &&
            !(s.limit & 0xfe00) && signed_bits(s.offset,8)>=-111 && signed_bits(s.offset,8)<=112 &&
            s.limit*6250 >= 300000 && !(s.vout&0xf000) && (s.current&0xf000)==0xe000 &&
            !(s.temp&0xf800) && s.vout>=300 && s.vout<=2000 && s.temp<=1500;
    };
    const bool ok=sample(a) && sample(b) && a.addr==b.addr && a.offset==b.offset && a.limit==b.limit &&
        (a.config&8)==(b.config&8) && std::abs(static_cast<int>(a.vout)-static_cast<int>(b.vout))<=500 &&
        std::abs(static_cast<int>(a.temp)-static_cast<int>(b.temp))<=100;
    route_=old; if (ok) vout=(a.vout+b.vout)/2.0; return ok;
}
std::vector<I2cCandidate> I2cControls::scan() {
    std::vector<I2cCandidate> found; if (owned_ || !transport_.bound()) return found;
    for (uint8_t port=0;port<8;++port) {
        auto test=[&](uint8_t address) { double v=0; I2cRoute route{port,address}; if (fingerprint(route,v)) found.push_back({route,v}); };
        test(0x20); for (int a=0x08;a<=0x77;++a) if (a!=0x20) test(static_cast<uint8_t>(a));
    }
    return found;
}
bool I2cControls::select(I2cRoute route) {
    if (owned_) return false;
    double vout=0; if (!fingerprint(route,vout)) return false;
    route_=route; gate_=verified_=false; initial_.reset(); current_owned_.reset(); last_status_.reset(); permitted_.clear(); return true;
}
bool I2cControls::capture(Mp2888Control& out) {
    if (!route_) return false;
    Mp2888Control s{}; s.route=*route_;
    if (!read(0x00,1,s.page) || !read(0xbe,1,s.address) || !read(0x21,2,s.command) ||
        !read(0xe1,2,s.mode) || !read(0x01,1,s.operation) || !read(0x45,2,s.pwm) ||
        !read(0x23,2,s.offset) || !read(0x2a,2,s.sense) || !read(0x2d,1,s.fine) ||
        !read(0x1f,2,s.minimum) || !read(0x24,2,s.maximum) || !shape(s)) return false;
    uint32_t page=0,address=0,mode=0,command=0;
    if (!read(0x00,1,page) || !read(0xbe,1,address) || !read(0xe1,2,mode) || !read(0x21,2,command) ||
        page!=s.page || address!=s.address || mode!=s.mode || command!=s.command) return false;
    out=s; return true;
}
I2cStatus I2cControls::status() {
    I2cStatus result; result.owned=owned_; result.recovery_pending=recovery_pending_; result.response_verified=verified_; Mp2888Control state{};
    if (!capture(state)) {
        if (last_status_) result=*last_status_;
        result.readable=false; result.stale=true; result.writable=false; result.request_eligible=false; result.owned=owned_;
        result.recovery_pending=recovery_pending_; result.response_verified=verified_; result.message="Last controller request retained; retry read"; return result;
    }
    result.readable=true; result.enabled=(state.mode&0x10)!=0;
    if (result.enabled) result.requested_mv=state.command*6.25;
    uint32_t vout=0; if (read(0x8b,2,vout) && vout>=300 && vout<=2000) result.controller_vout_mv=static_cast<double>(vout);
    int64_t low=0,high=0; if (bounds(state,low,high)) { result.min_mv=low/1000.0; result.max_mv=high/1000.0; result.capable=true; result.request_eligible=!recovery_pending_ &&
        ((!owned_ && !(state.mode&0x10)) || (owned_ && current_owned_ && same(state,*current_owned_))) &&
        (state.operation&0xf0)==0x80 && !(state.pwm&0x20); result.writable=gate_ && verified_ && result.request_eligible; }
    result.message="Controller-reported voltage is not a physical measurement"; last_status_=result; return result;
}
I2cResult I2cControls::transition(const Mp2888Control& from, const Mp2888Control& target) {
    if (!context(from,target)) return error("Controller context changed; no write issued");
    Mp2888Control expected=from; bool dispatched=false;
    auto write=[&](bool command) {
        Mp2888Control current{}; if (!capture(current) || !same(current,expected)) return false;
        Mp2888Control next=expected;
        if (command) next.command=target.command; else next.mode=target.mode;
        if (same(next,expected)) return true;
        permitted_.push_back(next); dispatched=true; // record possible result before dispatch
        bool accepted=false;
        try { accepted=transport_.write(route_->port,route_->addr7,command?0x21:0xe1,2,command?next.command:next.mode); }
        catch (...) { return false; }
        if (!transport_.bound() || !capture(current) || !same(current,next) || !accepted) return false;
        expected=next; return true;
    };
    const bool enabled=(target.mode&0x10)!=0;
    const bool ok=enabled ? (write(true) && write(false)) : (write(false) && write(true));
    if (!ok) { auto result = error("Controller transaction unverified; retain owned recovery and clock hold",dispatched); result.recovery_pending = owned_; return result; }
    Mp2888Control final{}; if (!capture(final) || !same(final,target)) { auto result = error("Controller final readback unavailable; retain recovery",dispatched); result.recovery_pending = owned_; return result; }
    I2cResult result; result.ok=true; result.may_have_changed=dispatched; result.recovery_pending=owned_; result.message="Controller command and mode read back exactly; physical voltage unverified"; return result;
}
I2cResult I2cControls::apply(double target_mv, bool clock_held, const std::function<void(const Mp2888Control&)>& before_write) {
    return apply_impl(target_mv, clock_held, before_write, false);
}
I2cResult I2cControls::apply_impl(double target_mv, bool clock_held, const std::function<void(const Mp2888Control&)>& before_write, bool verification) {
    if (!gate_ || (!verified_ && !verification) || !clock_held || !route_) return error("I2C gate, response verification, and owned clock hold are required");
    if (recovery_pending_) return error("Recover the uncertain owned request first");
    Mp2888Control before{}; if (!capture(before)) return error("Controller capture unavailable");
    if ((!owned_ && (before.mode & 0x10)) || (owned_ && (!current_owned_ || !same(before,*current_owned_))))
        return error("Foreign manual mode or changed owned request; no write issued");
    int64_t low=0,high=0; const double uv=target_mv*1000.0;
    if (!bounds(before,low,high) || !std::isfinite(uv) || uv<low || uv>high || std::trunc(uv/6250.0)!=uv/6250.0 ||
        (before.operation&0xf0)!=0x80 || (before.pwm&0x20)) return error("Target or controller configuration is unsupported");
    uint32_t state=0; if (!read(0x78,1,state) || (state&0x40)) return error("Controller status unavailable or output off");
    auto target=before; target.command=static_cast<uint32_t>(uv/6250.0); target.mode|=0x10;
    if (!owned_) {
        if (before_write) before_write(before); // no controller SET before caller records recovery
        initial_=before; permitted_.clear(); permitted_.push_back(before); owned_=true;
    }
    try {
        auto result = transition(before,target); recovery_pending_ = !result.ok;
        if (result.ok) current_owned_=target;
        result.recovery_pending = recovery_pending_; return result;
    } catch (...) { recovery_pending_=true; return error("Controller transaction raised; retain owned recovery and clock hold",true); }
}
I2cResult I2cControls::verify(bool clock_held, const I2cVerifyCallbacks& cb,
                               const std::function<void(const Mp2888Control&)>& before_write) {
    verified_=false;
    if (!gate_ || !clock_held || !route_ || owned_ || !cb.core_clock_mhz || !cb.sleep_ms)
        return error("Verification needs the I2C gate, an unowned controller, and a caller-owned stable low clock");
    Mp2888Control entry{}; int64_t low=0, high=0; uint32_t entry_status=0;
    if (!capture(entry) || (entry.mode&0x10) || !bounds(entry,low,high) ||
        (entry.operation&0xf0)!=0x80 || (entry.pwm&0x20) || !read(0x78,1,entry_status) || (entry_status&0x40))
        return error("Verification entry state is unavailable, foreign, or unsupported");
    int64_t first=std::max<int64_t>(900000,low);
    if (first+25000>high) first=((high-25000)/6250)*6250;
    if (first<low || first+25000>high) return error("Live limits cannot fit two verification commands");
    std::optional<double> clock;
    auto check=[&]() {
        if (!gate_ || !transport_.bound() || (cb.cancelled && cb.cancelled())) return false;
        const auto now=cb.core_clock_mhz();
        if (!now || !std::isfinite(*now) || *now<=0) return false;
        if (!clock) clock=*now;
        return *now==*clock;
    };
    auto sample=[&](const Mp2888Control& expected, double& median, double& noise) {
        std::vector<double> values; values.reserve(15);
        for (int i=0;i<15;++i) {
            Mp2888Control current{}; uint32_t status=0, vout=0;
            if (!check() || !capture(current) || !same(current,expected) || !read(0x78,1,status) ||
                (status&0x40) || ((status&0x3c)&~(entry_status&0x3c)) || !read(0x8b,2,vout) ||
                vout<300 || vout>1200 || !check()) return false;
            values.push_back(static_cast<double>(vout)); cb.sleep_ms(40);
        }
        std::sort(values.begin(),values.end()); median=values[7]; noise=values.back()-values.front(); return true;
    };
    double entry_median=0, entry_noise=0;
    try { if (!check() || !sample(entry,entry_median,entry_noise) || entry_noise>6.25) return error("Verification entry is noisy or unreadable; no write issued"); }
    catch (...) { return error("Verification callback failed; no write issued"); }
    double medians[3]{}, noises[3]{}; bool passed=true, attempted=false;
    try {
        const int64_t targets[3]={first,first+25000,first};
        for (int i=0;i<3 && passed;++i) {
            if (!check()) { passed=false; break; }
            auto action=apply_impl(targets[i]/1000.0,clock_held,before_write,true);
            attempted=attempted || action.may_have_changed;
            if (!action.ok || !current_owned_) { passed=false; break; }
            cb.sleep_ms(150);
            if (!sample(*current_owned_,medians[i],noises[i])) passed=false;
        }
    } catch (...) { passed=false; }
    const double noise=std::max({noises[0],noises[1],noises[2]});
    const double delta=std::min(medians[1]-medians[0],medians[1]-medians[2]);
    passed = passed && noise<=6.25 && std::abs(medians[2]-medians[0])<=7.0 && delta>=6.25 && delta>noise;
    if (owned_) {
        auto restored=restore_impl(clock_held,false);
        if (!restored.ok) { restored.ok=false; restored.may_have_changed=attempted || restored.may_have_changed; restored.recovery_pending=true; restored.message="Verification inconclusive; captured controller state not restored: "+restored.message; return restored; }
    }
    bool final_confirmed=false;
    try {
        double restored=0, noise_after=0; Mp2888Control final{};
        cb.sleep_ms(150);
        if (!sample(entry,restored,noise_after)) passed=false;
        final_confirmed=capture(final) && same(final,entry);
    } catch (...) { passed=false; }
    if (!final_confirmed) {
        recovery_pending_=owned_;
        auto failed=error("Verification final restoration is unconfirmed; retain clock hold",attempted);
        failed.recovery_pending=owned_; return failed;
    }
    owned_=false; recovery_pending_=false; initial_.reset(); current_owned_.reset(); permitted_.clear();
    I2cResult result; result.may_have_changed=attempted;
    if (!passed) { result.message="Verification inconclusive; captured entry was restored exactly"; return result; }    verified_=true; result.ok=true;
    result.message="A-B-A controller-reported response verified and entry restored; physical voltage requires independent measurement";
    return result;
}I2cResult I2cControls::restore(bool clock_held) { return restore_impl(clock_held,true); }
I2cResult I2cControls::restore_impl(bool clock_held, bool release_ownership) {
    if (!owned_ || !initial_ || !clock_held || !route_) return error("No owned request or clock hold available");
    Mp2888Control current{}; if (!capture(current)) { recovery_pending_=true; return error("Controller state unavailable; retain recovery",true); }
    if (std::none_of(permitted_.begin(),permitted_.end(),[&](const auto& p){return same(p,current);}))
        { recovery_pending_=true; return error("Controller state is outside owned recovery candidates",true); }
    I2cResult result;
    try { result=transition(current,*initial_); }
    catch (...) { recovery_pending_=true; return error("Controller recovery raised; retain clock hold",true); }
    if (result.ok) {
        recovery_pending_=false; current_owned_=*initial_;
        if (release_ownership) { owned_=false; initial_.reset(); current_owned_.reset(); permitted_.clear(); }
        result.recovery_pending=false; result.message="Captured controller command and mode restored; release clock hold afterward";
    }
    if (!result.ok) recovery_pending_=true;
    return result;
}
}
