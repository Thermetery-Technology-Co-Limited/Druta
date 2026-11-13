/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "../i2c_driver.h"
#include <cassert>
#include <cmath>
#include <map>
using namespace druta::ui;
struct Mock final : ControllerIo {
    std::map<int,std::vector<uint8_t>> regs; int transient=0, fail_write=0, writes=0; bool responds=true, bound=true;
    bool controller_bound() const override {return bound;}
    bool read_bytes(I2cRoute,uint8_t reg,size_t size,std::vector<uint8_t>& out) override {
        if(transient>0) {--transient;return false;}
        if(reg==0x8b && responds) {int n=regs[0x23][0]; if(n&128)n-=256; int v=int(std::lround(900+n*6.25)); out={uint8_t(v),uint8_t(v>>8)}; return size==2;}
        auto i=regs.find(reg); if(i==regs.end()||i->second.size()!=size)return false;out=i->second;return true;
    }
    bool write_bytes(I2cFamily family,I2cRoute,uint8_t reg,const std::vector<uint8_t>& b) override {assert(family==I2cFamily::mp2888 && reg==0x23 && b.size()==2); ++writes; regs[reg]=b; if(fail_write>0) {--fail_write;return false;}return true;}
    std::optional<uint32_t> architecture() const override {return 4;}
    std::optional<double> reference_mv() const override {return {};}
    std::optional<double> core_clock_mhz() const override {return 1000;}
    std::optional<ControllerOperatingPoint> operating_point() const override {return ControllerOperatingPoint{0,1000,3000};}
    void sleep_ms(unsigned) override {}
};
int main() {
    Mock m; m.regs={{0xbe,{0xa7}},{0x23,{0,0}},{0x24,{0xd0,0}},{0x8b,{0x84,3}},{0x8c,{0x10,0xe0}},{0x8d,{0x2c,1}},{0x44,{0,0}},{0xe1,{0x02,0x40}}};
    // Discovery must retain the candidate even when only the separate
    // manual PMBus recipe can currently operate it.
    Mock manual=m; manual.regs[0xe1][0]|=0x10;
    auto manual_candidate=make_mp2888_offset(manual,{3,0x27});
    assert(manual_candidate->probe() && !manual_candidate->read(false).capable);
    assert(!manual_candidate->verify({true,false}).ok && manual.writes==0);
    assert(!manual_candidate->apply({"offset",25,false},{true,false}).ok && manual.writes==0);
    manual.regs[0xe1][0]&=~0x10;
    assert(manual_candidate->read(false).capable && manual.writes==0);
    Mock partial=m; partial.regs.erase(0xe1);
    auto partial_candidate=make_mp2888_offset(partial,{3,0x27});
    assert(partial_candidate->probe() && !partial_candidate->read(false).readable);
    assert(!partial_candidate->verify({true,false}).ok && partial.writes==0);
    partial.regs[0xe1]=m.regs[0xe1];
    assert(partial_candidate->read(false).capable && partial.writes==0);
    auto d=make_mp2888_offset(m,{3,0x27}); m.transient=1; assert(d->probe());
    assert(!d->apply({"offset",25,false},{true,false}).ok && m.writes==0);
    assert(d->verify({true,false}).ok && m.regs[0x23][0]==0 && !d->owned());
    m.regs.erase(0x8c); m.regs.erase(0x8d); assert(d->read(false).capable);
    assert(d->apply({"offset",25,false},{true,false}).ok && m.regs[0x23][0]==4 && m.regs[0x23][1]==0);
    assert(d->restore().ok && m.regs[0x23][0]==0 && m.regs[0xe1][0]==2 && m.regs[0xe1][1]==0x40);
    m.fail_write=1; assert(!d->apply({"offset",25,false},{true,false}).ok && m.regs[0x23][0]==0 && !d->owned());
    m.regs[0xe1][0]|=0x10; int old=m.writes; assert(!d->apply({"offset",25,false},{true,false}).ok && m.writes==old); m.regs[0xe1][0]&=~0x10;
    m.regs[0x24]={0x90,0}; assert(!d->apply({"offset",25,false},{true,false}).ok && m.writes==old);
    m.regs[0x24]={0xd0,0}; assert(!d->apply({"offset",706.25,false},{true,true}).ok && m.writes==old);
    m.regs[0xbe][0]=0xa6; assert(!d->read(false).readable);
}
