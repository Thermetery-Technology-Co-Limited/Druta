/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "i2c_driver.h"
#include <cstdio>
using namespace druta::ui;
namespace {
int failures=0;
#define CHECK(x) do { if (!(x)) { ++failures; std::fprintf(stderr,"line %d: %s\n",__LINE__,#x); } } while (0)
struct Fake : ControllerIo {
    uint16_t word=0xa500; int page=1, scale=1, failed_reads=0, writes=0, base_raw=200; bool identity=true, respond=true, fail_write=false, drop_write=false, bound=true;
    bool controller_bound() const override { return bound; }
    bool read_bytes(I2cRoute,uint8_t reg,size_t n,std::vector<uint8_t>& out) override {
        if (failed_reads>0) { --failed_reads; return false; }
        switch(reg) {
        case 0xad: out={4,0x16,0xa8,uint8_t(identity?2:3),0}; break;
        case 0: out={uint8_t(page)}; break;
        case 0x29: out={0,uint8_t(scale<<2)}; break;
        case 0x22: out={uint8_t(word),uint8_t(word>>8)}; break;
        case 0x8b: { int signed_offset=int(word&255); if (signed_offset>=128) signed_offset-=256; int raw=base_raw+(respond?signed_offset:0); out={uint8_t(raw),uint8_t(raw>>8)}; break; }
        default: return false;
        }
        return out.size()==n;
    }
    bool write_bytes(I2cFamily f,I2cRoute,uint8_t reg,const std::vector<uint8_t>& b) override {
        CHECK(f==I2cFamily::mp29816 && reg==0x22 && b.size()==2); ++writes;
        if (!drop_write) word=uint16_t(b[0] | unsigned(b[1])<<8); return !fail_write;
    }
    std::optional<uint32_t> architecture() const override { return 9; }
    std::optional<double> reference_mv() const override { return {}; }
    std::optional<double> core_clock_mhz() const override { return {}; }
    std::optional<ControllerOperatingPoint> operating_point() const override { return ControllerOperatingPoint{0,300.0,1000.0}; }
    void sleep_ms(unsigned) override {}
};
}
int main() {
    Fake io; auto d=make_mp29816(io,{2,0x30});
    io.failed_reads=2; CHECK(d->probe());
    CHECK(d->read(false).readable && d->read(false).capable);
    CHECK(d->verify({true,false}).ok && io.word==0xa500 && !d->owned());
    CHECK(d->apply({"offset_mv",10,false},{true,false}).ok && io.word==0xa502 && d->owned());
    io.fail_write=true; io.drop_write=true;
    CHECK(!d->apply({"offset_mv",15,false},{true,false}).ok && io.word==0xa502 && d->owned());
    CHECK(!d->apply({"offset_mv",20,false},{true,false}).ok);
    io.fail_write=false; io.drop_write=false;
    CHECK(d->restore().ok && io.word==0xa500 && !d->owned());
    CHECK(!d->apply({"offset_mv",55,false},{true,false}).ok);
    io.scale=2; CHECK(!d->read(false).readable);
    io.scale=1; CHECK(d->apply({"offset_mv",10,false},{true,false}).ok);
    io.word=0xa503; const int before=io.writes;
    CHECK(!d->restore().ok && io.writes==before && d->owned());
    io.word=0xa502; CHECK(d->restore().ok);
    Fake xoc; auto wide=make_mp29816(xoc,{4,0x32});
    CHECK(wide->probe() && wide->verify({true,false}).ok);
    xoc.base_raw=280; const int before_xoc=xoc.writes;
    CHECK(!wide->apply({"offset_mv",10,false},{true,false}).ok && xoc.writes==before_xoc);
    CHECK(wide->apply({"offset_mv",10,false},{true,true}).ok && xoc.word==0xa502);
    CHECK(wide->restore().ok && xoc.word==0xa500);
    xoc.base_raw=60;
    CHECK(wide->apply({"offset_mv",10,false},{true,true}).ok && xoc.word==0xa502);
    CHECK(wide->restore().ok && xoc.word==0xa500);
    xoc.base_raw=400; const int at_ceiling=xoc.writes;
    CHECK(!wide->apply({"offset_mv",10,false},{true,true}).ok && xoc.writes==at_ceiling);
    CHECK(wide->apply({"offset_mv",-10,false},{true,true}).ok && xoc.word==0xa5fe);
    CHECK(wide->restore().ok && xoc.word==0xa500);
    xoc.base_raw=402; const int implausible=xoc.writes;
    CHECK(!wide->apply({"offset_mv",-10,false},{true,true}).ok && xoc.writes==implausible);
    Fake other; other.scale=2; auto telemetry=make_mp29816(other,{1,0x31}); CHECK(telemetry->probe()); CHECK(telemetry->read(false).readable && !telemetry->read(false).capable); CHECK(!telemetry->verify({true,false}).ok && !other.writes);
    Fake invalid; auto badroute=make_mp29816(invalid,{8,0x30}); CHECK(!badroute->probe() && !invalid.writes);
    Fake wrong; wrong.identity=false; auto absent=make_mp29816(wrong,{1,0x31}); CHECK(!absent->probe() && !wrong.writes);
    Fake flat; flat.respond=false; auto noresponse=make_mp29816(flat,{1,0x31}); CHECK(noresponse->probe()); CHECK(!noresponse->verify({true,false}).ok && flat.word==0xa500 && !noresponse->owned());
    Fake uncertain; uncertain.fail_write=true; auto recovery=make_mp29816(uncertain,{1,0x31}); CHECK(recovery->probe()); CHECK(!recovery->verify({true,false}).ok && recovery->owned()); uncertain.fail_write=false; CHECK(recovery->restore().ok && uncertain.word==0xa500);
    std::printf("i2c_mp29816_test: %d failures\n",failures); return failures?1:0;
}
