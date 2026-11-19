/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "../i2c_driver.h"
#include <cassert>
#include <cmath>
#include <map>
using namespace druta::ui;
struct Mock final : ControllerIo {
    std::map<int,std::vector<uint8_t>> regs; int transient=0, fail_write=0, fail_at=0; bool bind=true, vmon=false; int writes=0;
    bool controller_bound() const override {return bind;}
    bool read_bytes(I2cRoute,uint8_t reg,size_t count,std::vector<uint8_t>& out) override {if(transient>0) {--transient; return false;} if(reg==0xd7 && vmon) {double mv=(regs[0xd2][0]&8)?1600-(regs[0x21][0]-2)*6.25:1000; int raw=(23<<11)|int(std::lround(mv*0.512)); out={uint8_t(raw),uint8_t(raw>>8)}; return count==2;} auto i=regs.find(reg); if(i==regs.end() || i->second.size()!=count) return false; out=i->second; return true;}
    bool write_bytes(I2cFamily,I2cRoute,uint8_t reg,const std::vector<uint8_t>& bytes) override {++writes; regs[reg]=bytes; if(writes==fail_at) return false; if(fail_write>0) {--fail_write; return false;} return true;}
    std::optional<uint32_t> architecture() const override {return 2;}
    std::optional<double> reference_mv() const override {return {};}
    std::optional<double> core_clock_mhz() const override {return 1000;}
    std::optional<ControllerOperatingPoint> operating_point() const override {return ControllerOperatingPoint{0,1000,3000};}
    void sleep_ms(unsigned) override {}
};
int main() {
    Mock n; n.regs={{0x5d,{0x39}},{0x5e,{0x33}},{5,{0xa1}},{1,{0x81}},{2,{0x02}},{3,{0x83}}};
    auto dac=make_nct3933(n,{4,0x12}); n.transient=1; assert(dac->probe());
    auto view=dac->read(false); assert(view.fields.size()==3 && *view.fields[0].value==20 && *view.fields[1].value==-20);
    assert(!dac->apply({"out1",40,false},{false,false}).ok && n.writes==0);
    assert(dac->apply({"out1",40,false},{true,false}).ok); assert(n.regs[1][0]==0x82 && n.regs[2][0]==2 && n.regs[5][0]==0xa1);
    assert(dac->restore().ok && n.regs[1][0]==0x81);
    n.fail_write=1; assert(!dac->apply({"out3",40,false},{true,false}).ok); assert(n.regs[3][0]==0x83 && !dac->owned());
    n.fail_write=2; assert(!dac->apply({"out3",40,false},{true,false}).ok && dac->owned());
    n.regs[3][0]=0x88; int count=n.writes; assert(!dac->restore().ok && n.writes==count);
    n.regs[3][0]=0x84; assert(dac->restore().ok && n.regs[3][0]==0x83);
    n.regs[0x5e]={0x34}; assert(!dac->probe());
    Mock p; p.regs={{0x99,{0x41}},{0x9a,{0x08,0x02}},{0x9b,{0x77}},{0x20,{32}},{0x21,{0x44,0}},{0xd2,{0xa5}},{0xd3,{0x52}}};
    auto vid=make_ncp4206(p,{3,0x27}); assert(vid->probe()); assert(vid->read(false).fields[0].key=="voltage");
    assert(!vid->apply({"voltage",1000,false},{true,false}).ok && p.writes==0);
    // Auto preserves unrelated configuration bits and restores the captured command.
    assert(vid->apply({"voltage",0,true},{true,false}).ok); assert(p.regs[0xd2][0]==0xa5 && p.regs[0xd3][0]==0x52 && p.regs[0x21][0]==0);
    assert(vid->restore().ok && p.regs[0x21][0]==0x44 && p.regs[0xd2][0]==0xa5 && p.regs[0xd3][0]==0x52);
    p.regs[0xd2][0]|=8; assert(!vid->probe());
    assert(vid->apply({"voltage",0,true},{true,false}).ok);
    assert(p.regs[0xd2][0]==0xa5 && p.regs[0xd3][0]==0x52 && p.regs[0x21][0]==0x44);
    p.regs[0x9b][0]=0x78; assert(!vid->probe());
    for(int failing=1;failing<=3;++failing) {
        Mock q; q.regs={{0x99,{0x41}},{0x9a,{0x98,0x32}},{0x9b,{0x55}},{0x20,{32}},{0x21,{0x44,0}},{0xd2,{0xad}},{0xd3,{0x5a}}};
        auto driver=make_ncp4206(q,{2,0x20}); assert(driver->probe()); q.fail_at=failing;
        assert(!driver->apply({"voltage",0,true},{true,false}).ok);
        assert(q.regs[0x21][0]==0x44 && q.regs[0xd2][0]==0xad && q.regs[0xd3][0]==0x5a && !driver->owned());
    }
    for(int failing=1;failing<=3;++failing) {
        Mock q; q.vmon=true; q.regs={{0x99,{0x41}},{0x9a,{0x98,0x32}},{0x9b,{0x55}},{0x20,{32}},{0x21,{0x44,0}},{0xd2,{0xa5}},{0xd3,{0x52}}};
        auto driver=make_ncp4206(q,{2,0x20}); assert(driver->probe()); q.fail_at=failing;
        assert(!driver->verify({true,false}).ok && q.writes>=failing);
        assert(q.regs[0x21][0]==0x44 && q.regs[0xd2][0]==0xa5 && q.regs[0xd3][0]==0x52 && !driver->owned());
    }
    Mock q; q.regs={{0x99,{0x41}},{0x9a,{0x98,0x32}},{0x9b,{0x55}},{0x20,{32}},{0x21,{0x44,0}},{0xd2,{0xad}},{0xd3,{0x5a}}};
    auto uncertain=make_ncp4206(q,{2,0x20}); assert(uncertain->probe()); q.fail_write=2;
    assert(!uncertain->apply({"voltage",0,true},{true,false}).ok && uncertain->owned());
    q.regs[0x21][0]=0x45; count=q.writes; assert(!uncertain->restore().ok && q.writes==count);
    q.regs[0x21][0]=0x44; assert(uncertain->restore().ok && q.regs[0xd2][0]==0xad && q.regs[0xd3][0]==0x5a);
}
