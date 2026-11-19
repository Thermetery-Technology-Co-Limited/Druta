// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "i2c_driver.h"
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <stdexcept>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
struct NcpIo final:ControllerIo {
    std::map<uint8_t,unsigned> registers{{0x99,0x41},{0x9a,0x3298},{0x9b,1},{0x20,32},{0x21,0xFD},{0xd2,0xA1},{0xd3,0x52}};
    bool bound=true,block_reads=false,throw_after_write=false;unsigned writes=0,fail_at=0,corrupt_at=0,transient=0;
    std::vector<uint8_t> order;std::vector<std::vector<uint8_t>> packets;
    bool controller_bound()const override{return bound;}
    bool read_bytes(I2cRoute,uint8_t reg,size_t count,std::vector<uint8_t>& out)override{
        if(block_reads)throw std::runtime_error("Injected read failure");if(transient){--transient;return false;}
        unsigned value=0;if(reg==0xd7){const auto code=registers.at(0x21);const bool enabled=(registers.at(0xd2)&8)&&(registers.at(0xd3)&8);const double mv=enabled&&code>=2&&code<=198?1600.0-(code-2)*6.25:900.0;
            value=(23u<<11)|(static_cast<unsigned>(std::lround(mv*512/1000))&2047u);}
        else{if(!registers.count(reg))return false;value=registers.at(reg);}
        out.resize(count);for(size_t i=0;i<count;++i)out[i]=static_cast<uint8_t>(value>>(8*i));return true;
    }
    bool write_bytes(I2cFamily family,I2cRoute,uint8_t reg,const std::vector<uint8_t>& bytes)override{
        CHECK(family==I2cFamily::ncp4206&&(reg==0x21||reg==0xd2||reg==0xd3));CHECK(bytes.size()==(reg==0x21?2u:1u));
        ++writes;order.push_back(reg);packets.push_back(bytes);unsigned value=0;for(size_t i=0;i<bytes.size();++i)value|=unsigned(bytes[i])<<(8*i);registers[reg]=value;
        CHECK(!((registers[0xd2]|registers[0xd3])&8)||registers[0x21]<=198); // Off commands only after both enable bits clear.
        if(writes==corrupt_at)registers[0xd3]^=0x80;
        if(throw_after_write){throw_after_write=false;block_reads=true;throw std::runtime_error("Accepted write then transport exception");}return writes!=fail_at;
    }
    std::optional<uint32_t> architecture()const override{return 3;}
    std::optional<double> reference_mv()const override{return {};}
    std::optional<double> core_clock_mhz()const override{return 300.0;}
    std::optional<ControllerOperatingPoint> operating_point()const override{return ControllerOperatingPoint{0,300.0,3000.0};}
    void sleep_ms(unsigned)override{}
    void clear(){writes=fail_at=corrupt_at=0;order.clear();packets.clear();}
};
static bool equal(const Ncp4206State& a,const Ncp4206State& b){return a.enabled==b.enabled&&a.command==b.command;}
static Ncp4206State capture(ControllerDriver& driver){Ncp4206State s;CHECK(driver.capture_ncp4206(s).ok);return s;}
static std::unique_ptr<ControllerDriver> verified(NcpIo& io,I2cRoute route={2,0x20}){auto driver=make_ncp4206(io,route);const auto result=driver->verify({true,false});if(!result.ok)std::fprintf(stderr,"verify: %s\n",result.message.c_str());CHECK(result.ok&&!driver->owned());io.clear();return driver;}
int main(){
    {NcpIo io;auto driver=verified(io);io.transient=1;const auto before=capture(*driver);CHECK(!before.enabled&&before.command==0xFD&&!driver->needs_clock_clamp());Ncp4206Plan plan;
        CHECK(driver->preview_ncp4206({true,54},false,plan).ok&&io.writes==0);Ncp4206Receipt receipt;unsigned journals=0;
        CHECK(!driver->apply_ncp4206(plan,{false,false},{}).ok&&io.writes==0);CHECK(!driver->apply_ncp4206(plan,{true,true},{}).ok&&io.writes==0);
        io.registers[0xd2]|=0x10;CHECK(driver->apply_ncp4206(plan,{true,false},[&](const auto& r){CHECK(io.writes==0);++journals;receipt=r;}).ok);
        CHECK(journals==1&&io.order==std::vector<uint8_t>({0x21,0xd2,0xd3})&&io.packets[0]==std::vector<uint8_t>({54,0})&&io.registers[0xd2]==0xB9&&driver->owned()&&driver->read(false).owned);
        CHECK(driver->verify_ncp4206(plan).ok&&driver->verify_ncp4206(receipt).ok);const auto writes=io.writes;CHECK(!driver->apply({"voltage",1000,false},{true,false}).ok&&io.writes==writes);
        io.registers[0xd3]^=0x40;CHECK(driver->restore_ncp4206(receipt).ok&&equal(capture(*driver),before)&&io.registers[0xd2]==0xB1&&io.registers[0xd3]==0x12&&!driver->owned());
        CHECK(driver->verify_ncp4206(receipt,true).ok&&driver->restore_ncp4206(receipt).ok);
    }
    {NcpIo io;io.registers[0x21]=110;io.registers[0xd2]|=8;io.registers[0xd3]|=8;auto driver=verified(io);const auto before=capture(*driver);Ncp4206Plan plan;
        CHECK(driver->preview_ncp4206({false,255},false,plan).ok);Ncp4206Receipt receipt;CHECK(driver->apply_ncp4206(plan,{true,false},[&](const auto& r){receipt=r;}).ok);
        CHECK(io.order==std::vector<uint8_t>({0xd2,0xd3,0x21})&&equal(capture(*driver),{false,255}));CHECK(driver->restore().ok&&equal(capture(*driver),before));
        CHECK(driver->apply({"voltage",0,true},{true,false}).ok&&equal(capture(*driver),{false,0})); // Scalar Auto still clears its retained command explicitly.
        const auto writes=io.writes;CHECK(!driver->restore_ncp4206(receipt).ok&&io.writes==writes);CHECK(driver->restore().ok);
    }
    {NcpIo io;auto driver=make_ncp4206(io,{7,0x42});const auto before=capture(*driver);Ncp4206Plan plan;CHECK(driver->preview_ncp4206(before,false,plan).ok);unsigned journals=0;
        CHECK(driver->apply_ncp4206(plan,{false,false},[&](const auto&){++journals;}).ok&&journals==0&&io.writes==0&&!driver->owned());io.registers[0x21]=254;CHECK(!driver->verify_ncp4206(plan).ok);
        CHECK(!driver->preview_ncp4206({true,54},false,plan).ok&&!plan.state&&io.writes==0); // Missing response verification, not a manufactured request.
    }
    {NcpIo io;io.registers[0x9a]=0x0208;io.registers[0x9b]=0xA7;auto driver=verified(io,{7,0x42});Ncp4206Plan plan;
        CHECK(!driver->preview_ncp4206({true,2},false,plan).ok&&!plan.state);CHECK(driver->preview_ncp4206({true,2},true,plan).ok);
        CHECK(!driver->preview_ncp4206({true,198},true,plan).ok&&!plan.state);CHECK(!driver->preview_ncp4206({true,0},true,plan).ok);
        CHECK(driver->preview_ncp4206({false,1},false,plan).ok);Ncp4206Receipt receipt;CHECK(driver->apply_ncp4206(plan,{true,false},[&](const auto& r){receipt=r;}).ok&&equal(capture(*driver),{false,1}));CHECK(driver->restore_ncp4206(receipt).ok);
    }
    {NcpIo io,other;auto driver=verified(io),foreign=verified(other);Ncp4206Plan plan;CHECK(driver->preview_ncp4206({true,54},false,plan).ok);CHECK(!foreign->apply_ncp4206(plan,{true,false},{}).ok&&other.writes==0);
        io.registers[0x9b]=2;CHECK(!driver->apply_ncp4206(plan,{true,false},{}).ok&&io.writes==0);io.registers[0x9b]=1;io.registers[0x20]=0;CHECK(!driver->apply_ncp4206(plan,{true,false},{}).ok&&io.writes==0);
    }
    {NcpIo io;auto driver=verified(io);Ncp4206Plan plan;CHECK(driver->preview_ncp4206({true,54},false,plan).ok);CHECK(!driver->apply_ncp4206(plan,{true,false},[](const auto&){throw std::runtime_error("No journal");}).ok&&io.writes==0&&!driver->owned());
        CHECK(driver->apply({"voltage",1000,false},{true,false}).ok);const auto writes=io.writes;CHECK(!driver->apply_ncp4206(plan,{true,false},{}).ok&&io.writes==writes&&!driver->preview_ncp4206({false,1},false,plan).ok);CHECK(driver->restore().ok);
    }
    for(unsigned fail=1;fail<=3;++fail){NcpIo io;auto driver=verified(io);const auto before=capture(*driver);Ncp4206Plan plan;CHECK(driver->preview_ncp4206({true,54},false,plan).ok);Ncp4206Receipt receipt;io.fail_at=fail;
        const auto result=driver->apply_ncp4206(plan,{true,false},[&](const auto& r){receipt=r;});CHECK(!result.ok&&result.may_have_changed&&result.recovery_pending&&driver->owned()&&io.writes==fail);
        io.fail_at=0;CHECK(driver->restore_ncp4206(receipt).ok&&equal(capture(*driver),before)&&driver->verify_ncp4206(receipt,true).ok);
    }
    {NcpIo io;auto driver=verified(io);Ncp4206Plan plan;CHECK(driver->preview_ncp4206({true,54},false,plan).ok);Ncp4206Receipt receipt;io.fail_at=1;
        CHECK(!driver->apply_ncp4206(plan,{true,false},[&](const auto& r){receipt=r;}).ok&&io.writes==1);io.fail_at=0;io.registers[0xd2]|=8;
        CHECK(!driver->restore_ncp4206(receipt).ok&&io.writes==1);io.registers[0xd2]&=~8u;CHECK(driver->restore_ncp4206(receipt).ok&&!driver->owned());
    }
    {NcpIo io;auto driver=verified(io);const auto before=capture(*driver);Ncp4206Plan plan;CHECK(driver->preview_ncp4206({true,54},false,plan).ok);Ncp4206Receipt receipt;io.throw_after_write=true;
        CHECK(!driver->apply_ncp4206(plan,{true,false},[&](const auto& r){receipt=r;}).ok&&io.writes==1&&driver->owned());io.block_reads=false;CHECK(driver->restore().ok&&equal(capture(*driver),before));
    }
    {NcpIo io;auto driver=verified(io);Ncp4206Plan plan;CHECK(driver->preview_ncp4206({true,54},false,plan).ok);Ncp4206Receipt receipt;CHECK(driver->apply_ncp4206(plan,{true,false},[&](const auto& r){receipt=r;}).ok);
        io.registers[0x21]=55;const auto writes=io.writes;CHECK(!driver->restore_ncp4206(receipt).ok&&io.writes==writes);io.registers[0x21]=54;io.registers[0xd2]&=~8u;CHECK(!driver->restore_ncp4206(receipt).ok&&io.writes==writes);io.registers[0xd2]|=8;
        CHECK(driver->restore_ncp4206(receipt).ok);CHECK(driver->apply({"voltage",1000,false},{true,false}).ok);const auto after=io.writes;CHECK(!driver->restore_ncp4206(receipt).ok&&io.writes==after);CHECK(driver->restore().ok);
    }
    {NcpIo io;auto driver=verified(io);const auto before=capture(*driver);Ncp4206Plan plan;CHECK(driver->preview_ncp4206({true,54},false,plan).ok);Ncp4206Receipt receipt;io.corrupt_at=1;
        CHECK(!driver->apply_ncp4206(plan,{true,false},[&](const auto& r){receipt=r;}).ok&&io.writes==1&&driver->owned());io.corrupt_at=0;CHECK(driver->restore_ncp4206(receipt).ok&&equal(capture(*driver),before)&&io.registers[0xd3]==0xD2);
    }
    {NcpIo io;auto driver=verified(io);Ncp4206Plan plan;CHECK(driver->preview_ncp4206({true,54},false,plan).ok);Ncp4206Receipt receipt;CHECK(driver->apply_ncp4206(plan,{true,false},[&](const auto& r){receipt=r;}).ok);
        io.fail_at=io.writes+1;CHECK(!driver->restore_ncp4206(receipt).ok&&driver->owned());io.fail_at=0;CHECK(driver->restore_ncp4206(receipt).ok&&!driver->owned());
    }
    {NcpIo io;auto driver=verified(io);Ncp4206Plan plan;CHECK(driver->preview_ncp4206({true,54},false,plan).ok);io.registers[0xd2]|=8;CHECK(!driver->apply_ncp4206(plan,{true,false},{}).ok&&io.writes==0);io.registers[0xd2]&=~8u;io.registers[0x21]=256;Ncp4206State state;CHECK(!driver->capture_ncp4206(state).ok&&io.writes==0);}
    std::printf("%u NCP4206 profile checks passed\n",checks);
}
