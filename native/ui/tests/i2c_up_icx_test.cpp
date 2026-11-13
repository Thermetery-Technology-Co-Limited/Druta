/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "../i2c_driver.h"
#include <cassert>
#include <map>
using namespace druta::ui;
struct Mock final : ControllerIo {
    std::map<int,std::vector<uint8_t>> regs; std::map<int,int> failures; int writes=0; bool fail_write=false, fail_before_write=false, bound=true; uint32_t arch=7; bool response=false;
    bool controller_bound() const override { return bound; }
    bool read_bytes(I2cRoute,uint8_t reg,size_t size,std::vector<uint8_t>& out) override {
        if(failures[reg]>0) { --failures[reg]; return false; }
        auto it=regs.find(reg); if(it==regs.end()||it->second.size()!=size) return false;
        out=it->second;
        if(reg==0x2d && response && (regs[0x2a][0]&0x40)) out[0]=uint8_t(80+(regs[0x0a][0]>>4));
        return true;
    }
    bool write_bytes(I2cFamily family,I2cRoute,uint8_t reg,const std::vector<uint8_t>& data) override {
        ++writes;
        if(fail_before_write) { fail_before_write=false; return false; }
        if(family==I2cFamily::up9512r) regs[reg]=data;
        else if(reg==0xb2) regs[0xb2][1]=0xfc;
        else if(reg==0x10) { regs[0x19][1]=data[1]; regs[0x19][2]=data[2]; regs[0x19][3]=data[3]; }
        else if(reg==0x18) { regs[0x19][8]=data[2]; regs[0x19][9]=data[3]; }
        else { int state=reg==0x11?0x1a:(reg==0x12||reg==0x1e?0x1b:0x1c); int index=(reg==0x1e||reg==0x1f)?2:1; regs[state][index]=data[1]; }
        if(fail_write) { fail_write=false; return false; } return true;
    }
    std::optional<uint32_t> architecture() const override { return arch; }
    std::optional<double> reference_mv() const override { return {}; }
    std::optional<double> core_clock_mhz() const override { return 1000; }
    std::optional<ControllerOperatingPoint> operating_point() const override { return ControllerOperatingPoint{0,1000,3000}; }
    void sleep_ms(unsigned) override {}
};
int main() {
    Mock up; up.regs={{0x27,{0}},{0x28,{0x2b}},{0x0a,{0}},{0x0b,{0}},{0x0c,{0x0a}},{0x2a,{0x10}},{0x39,{0x94}},{0x2d,{80}},{0x2c,{42}}};
    auto u=make_up9512r(up,{3,0x25}); assert(u->probe()); up.failures[0x2c]=3;
    assert(u->read(false).fields.size()==1); assert(!u->apply({"offset",10,false},{true,false}).ok);
    up.response=true; assert(u->verify({true,false}).ok); assert(up.regs[0x0c][0]==0x0a && up.regs[0x2a][0]==0x10);
    up.failures[0x27]=1; assert(u->apply({"offset",20,false},{true,false}).ok);
    assert(up.regs[0x0a][0]==0x22 && up.regs[0x0c][0]==0x2a && up.regs[0x2a][0]==0x50);
    assert(u->restore().ok && up.regs[0x0c][0]==0x0a && up.regs[0x2a][0]==0x10);
    up.fail_write=true; assert(!u->apply({"offset",10,false},{true,false}).ok); assert(!u->owned() && up.regs[0x0a][0]==0);
    up.regs[0x28][0]=0x2c; assert(!u->probe());
    Mock icx; icx.regs={{0xb1,{5,0xfc,0x2d,0x13,0x77,0x42}},{0xb2,{4,0xfe,0,0,0}},{0x19,{9,0,0,0,0xaa,0xbb,0xcc,0xdd,0x04,0x01}},{0x1a,{2,4,0xaa}},{0x1b,{2,1,0}},{0x1c,{2,5,6}}};
    auto c=make_icx(icx,{1,0x2d}); assert(c->probe()); icx.failures[0x1a]=3;
    auto view=c->read(false); assert(view.fields.size()==6); assert(c->verify({true,false}).ok);
    assert(c->apply({"frequency_nvvdd",700,false},{true,false}).ok); assert(icx.regs[0x1c][1]==7 && icx.regs[0x1c][2]==6);
    assert(c->apply({"nvvdd",1000,false},{true,false}).ok); assert(icx.regs[0x19][4]==0xaa && icx.regs[0x19][7]==0xdd);
    assert(c->restore().ok); assert(icx.regs[0x1c][1]==5 && icx.regs[0x19][1]==0 && icx.regs[0xb2][1]==0xfc);
    icx.fail_write=true; assert(!c->apply({"ocp_fbvdd",1,false},{true,false}).ok); assert(c->owned()); assert(c->restore().ok && icx.regs[0x1b][2]==0);
    assert(c->apply({"frequency_nvvdd",700,false},{true,false}).ok);
    icx.fail_before_write=true; assert(!c->apply({"frequency_nvvdd",800,false},{true,false}).ok);
    assert(icx.regs[0x1c][1]==7); icx.regs[0x1c][1]=9;
    assert(!c->restore().ok && icx.regs[0x1c][1]==9);
    icx.regs[0x1c][1]=7; assert(c->restore().ok && icx.regs[0x1c][1]==5);
    icx.regs[0x19][8]=1; icx.regs[0x19][9]=0; int before_writes=icx.writes;
    assert(!c->apply({"fbvdd",1300,false},{true,false}).ok && icx.writes==before_writes);
    assert(icx.regs[0x19][8]==1 && icx.regs[0x19][9]==0);
    icx.arch=6; assert(!c->probe()); icx.arch=7; icx.regs[0xb1][2]=0x2e; assert(!c->probe());
}
