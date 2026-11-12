/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "i2c_driver.h"
#include <algorithm>
#include <array>
#include <cmath>
#include <set>

namespace druta::ui {
namespace {
class Mp29816 final : public ControllerDriver {
    ControllerIo& io_; I2cRoute route_; int page_ = -1, scale_ = -1;
    bool verified_ = false, owned_ = false, pending_ = false; uint16_t entry_ = 0, expected_ = 0;
    std::set<uint16_t> accepted_;
    static constexpr std::array<double,8> scales_{{6.25,5,2.5,2,1,1.0/256,1.0/512,1.0/1024}};
    bool read_reg(uint8_t reg, size_t n, std::vector<uint8_t>& out) {
        for (unsigned i=0;i<3;++i) {
            std::vector<uint8_t> v;
            if (io_.controller_bound() && io_.read_bytes(route_,reg,n,v) && v.size()==n && io_.controller_bound()) { out=std::move(v); return true; }
            if (i<2) io_.sleep_ms(25);
        }
        return false;
    }
    bool layout(int& page,int& scale) {
        std::vector<uint8_t> id,p,s;
        if (route_.port > 7 || route_.addr7 < 0x08 || route_.addr7 > 0x77) return false;
        if (!read_reg(0xad,5,id) || id != std::vector<uint8_t>({4,0x16,0xa8,2,0}) ||
            !read_reg(0,1,p) || p[0]>1 || !read_reg(0x29,2,s)) return false;
        page=p[0]; scale=((s[0] | unsigned(s[1])<<8)>>10)&7; return true;
    }
    bool present() { int p,s; return layout(p,s) && p==page_ && s==scale_; }
    bool word(uint16_t& x) { std::vector<uint8_t> b; if (!read_reg(0x22,2,b)) return false; x=uint16_t(b[0] | unsigned(b[1])<<8); return true; }
    bool voltage(double& x) { std::vector<uint8_t> b; if (!read_reg(0x8b,2,b)) return false; x=((b[0] | unsigned(b[1])<<8)&0xfff)*scales_[scale_]; return std::isfinite(x); }
    static double offset(uint16_t x) { const int v=x&255; return (v<128 ? v : v-256)*5.0; }
    bool point_equal(const ControllerOperatingPoint& a,const ControllerOperatingPoint& b) {
        return a.pstate>=0 && a.pstate==b.pstate && a.core_mhz==b.core_mhz && a.memory_mhz==b.memory_mhz;
    }
    bool samples(const ControllerOperatingPoint& point, uint16_t expected, double& median, double& noise) {
        std::array<double,5> v{};
        for (double& x:v) { auto p=io_.operating_point(); uint16_t current=0; if (!p || !point_equal(*p,point) || !present() || !word(current) || current!=expected || !voltage(x)) return false; io_.sleep_ms(20); }
        auto p=io_.operating_point(); if (!p || !point_equal(*p,point)) return false;
        std::sort(v.begin(),v.end()); median=v[2]; noise=v.back()-v.front(); return true;
    }
    I2cResult transition(uint16_t from,uint16_t to) {
        uint16_t current=0;
        if (!present() || !word(current) || current!=from || !present()) return {false,false,owned_,"Controller identity or offset changed before write"};
        if (from==to) return {true,false,pending_,"Offset already matches"};
        if (!owned_) { entry_=from; owned_=true; }
        accepted_.clear(); accepted_.insert(from); accepted_.insert(to); pending_=true;
        const bool sent=io_.write_bytes(I2cFamily::mp29816,route_,0x22,{uint8_t(to),uint8_t(to>>8)});
        uint16_t back=0;
        if (!sent || !present() || !word(back) || back!=to || !present()) return {false,true,true,"Offset write unverified; restore captured entry"};
        expected_=to; accepted_.clear(); accepted_.insert(to); pending_=false;
        return {true,true,false,"Offset word read back exactly"};
    }
    I2cResult set(double mv,bool xoc) {
        if (pending_) return {false,false,true,"Restore uncertain offset before another request"};
        if (!std::isfinite(mv) || std::abs(mv)>2000 || (!xoc && (mv < -50 || mv > 50)) || mv < -640 || mv > 635 || std::abs(mv/5-std::round(mv/5))>1e-8)
            return {false,false,owned_,"Offset outside the supported 5 mV steps or bounds"};
        uint16_t cur=0; double v=0;
        if (!present() || !word(cur) || !voltage(v) || v<(xoc?300:400) || v>(xoc?2000:1300)) return {false,false,owned_,"Controller state or plausible VOUT unavailable"};
        if (owned_ && cur!=expected_) return {false,false,true,"Offset changed externally; recovery retained"};
        double predicted=v-offset(cur)+mv;
        if (predicted>2000 || (!xoc && predicted>1200)) return {false,false,owned_,"Estimated rail would exceed the configured ceiling"};
        auto next=uint16_t((cur & 0xff00) | (int(std::lround(mv/5)) & 255));
        return transition(cur,next);
    }
public:
    Mp29816(ControllerIo& io,I2cRoute r):io_(io),route_(r) {}
    bool probe() override { int p,s; if (!layout(p,s)) return false; int p2,s2; if (!layout(p2,s2) || p!=p2 || s!=s2) return false; page_=p; scale_=s; return true; }
    ControllerView read(bool xoc) override {
        ControllerView v; v.name="MP29816 PAGE "+std::to_string(page_)+" output"; v.owned=owned_; v.recovery=pending_; v.verified=verified_; v.capable=scale_==1;
        double mv=0; uint16_t w=0;
        if (!present() || !voltage(mv)) { v.stale=true; v.message="Controller identity, PAGE, scale or telemetry unavailable; retry"; return v; }
        v.readable=true; v.readings.emplace_back("Controller VOUT",std::to_string(mv)+" mV (physical rail unassigned)");
        if (scale_==1 && word(w)) { I2cField f; f.key="offset_mv"; f.label="Offset"; f.unit="mV"; f.value=offset(w); f.minimum=xoc?-640:-50; f.maximum=xoc?635:50; f.step=5; f.writable=verified_; f.resettable=true; v.fields.push_back(f); }
        v.message=scale_==1 ? "Verify response at the held operating point before applying" : "Telemetry only: offset encoding unknown for this scale"; return v;
    }
    I2cResult verify(const ControllerContext& c) override {
        if (!c.gate || scale_!=1 || owned_ || !present()) return {false,false,owned_,"Enable controls and select a supported idle controller"};
        auto point=io_.operating_point(); uint16_t entry=0; double base=0,noise=0;
        if (!point || point->pstate<0 || !word(entry) || !samples(*point,entry,base,noise)) return {false,false,false,"Stable held operating point and controller baseline required"};
        bool hit=false; double high=0, high_noise=0; std::string failure;
        for (int rung : {5,10,15,20}) {
            auto p=io_.operating_point(); if (!p || !point_equal(*p,*point)) { failure="Operating point changed"; break; }
            auto r=set(offset(entry)+rung,c.xoc); if (!r.ok) { failure=r.message; break; }
            io_.sleep_ms(200); double measured=0,n=0;
            if (!samples(*point,expected_,measured,n)) { failure="Response sampling failed"; break; }
            if (measured-base>=5 && measured-base>std::max(noise,n)) { hit=true; high=measured; high_noise=n; break; }
        }
        auto restored=restore(); if (!restored.ok) return restored;
        double after=0, after_noise=0;
        if (!failure.empty() || !hit || !samples(*point,entry,after,after_noise) || std::abs(after-base)>std::max({5.0,noise,after_noise}) || high-after<5 || high-after<=std::max(high_noise,after_noise)) {
            verified_=false; return {false,true,false,failure.empty()?"Response or return to baseline inconclusive; entry restored":failure+"; entry restored"};
        }
        verified_=true; return {true,true,false,"Response and exact entry restoration verified at this operating point"};
    }
    I2cResult apply(const ControllerRequest& r,const ControllerContext& c) override {
        if (!c.gate || !verified_ || r.key!="offset_mv") return {false,false,owned_,"Verify and enable this offset first"};
        return set(r.reset?0:r.value,c.xoc);
    }
    I2cResult restore() override {
        if (!owned_) return {true,false,false,"No owned offset"};
        uint16_t cur=0;
        if (!present() || !word(cur)) return {false,false,true,"Entry restoration unavailable; retry"};
        if (cur!=entry_) {
            if (!accepted_.count(cur)) return {false,false,true,"Offset changed externally; restoration refused"};
            accepted_.insert(entry_); pending_=true;
            if (!io_.write_bytes(I2cFamily::mp29816,route_,0x22,{uint8_t(entry_),uint8_t(entry_>>8)})) return {false,true,true,"Restoration write unverified"};
        }
        uint16_t back=0; if (!present() || !word(back) || back!=entry_ || !present()) return {false,true,true,"Entry readback unverified"};
        owned_=false; pending_=false; accepted_.clear(); return {true,true,false,"Captured entry restored exactly"};
    }
    bool owned() const override { return owned_; }
};
}
std::unique_ptr<ControllerDriver> make_mp29816(ControllerIo& io,I2cRoute route) { return std::make_unique<Mp29816>(io,route); }
}
