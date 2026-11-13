// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "i2c_driver.h"
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <stdexcept>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
struct MpIo final:ControllerIo {
    uint16_t word=0xA502,scale=0x8403;uint8_t page=0;double base=1000;
    bool bound=true,identity=true,block=false,throw_after=false;unsigned writes=0,fail_at=0,transient=0;
    bool controller_bound()const override{return bound;}
    bool read_bytes(I2cRoute,uint8_t reg,size_t count,std::vector<uint8_t>& out)override{
        if(block)throw std::runtime_error("Read unavailable");if(transient){--transient;return false;}
        if(reg==0xad){out=identity?std::vector<uint8_t>{4,0x16,0xa8,2,0}:std::vector<uint8_t>{4,0,0,0,0};return true;}
        unsigned value=0;if(reg==0)value=page;else if(reg==0x29)value=scale;else if(reg==0x22)value=word;
        else if(reg==0x8b){const int raw=word&255;const auto mv=base+(raw<128?raw:raw-256)*5.0;value=0xA000u|static_cast<unsigned>(std::lround(mv/5));}else return false;
        out.resize(count);for(size_t i=0;i<count;++i)out[i]=static_cast<uint8_t>(value>>(8*i));return true;
    }
    bool write_bytes(I2cFamily family,I2cRoute,uint8_t reg,const std::vector<uint8_t>& bytes)override{
        CHECK(family==I2cFamily::mp29816&&reg==0x22&&bytes.size()==2);++writes;word=uint16_t(bytes[0]|unsigned(bytes[1])<<8);
        if(throw_after){throw_after=false;block=true;throw std::runtime_error("Accepted then transport failed");}return writes!=fail_at;
    }
    std::optional<uint32_t> architecture()const override{return 7;}
    std::optional<double> reference_mv()const override{return {};}
    std::optional<double> core_clock_mhz()const override{return 300.0;}
    std::optional<ControllerOperatingPoint> operating_point()const override{return ControllerOperatingPoint{0,300.0,3000.0};}
    void sleep_ms(unsigned)override{}
};
static std::unique_ptr<ControllerDriver> qualified(MpIo& io,I2cRoute route={7,0x42}){
    auto driver=make_mp29816(io,route);CHECK(driver->probe());const auto result=driver->verify({true,false});
    if(!result.ok)std::fprintf(stderr,"Verify: %s\n",result.message.c_str());CHECK(result.ok&&!driver->owned()&&io.word==0xA502);io.writes=0;return driver;
}
struct WrongPlan:OffsetProfilePlanState{};
struct WrongReceipt:OffsetProfileReceiptState{};
int main(){
    for(uint8_t page:{uint8_t(0),uint8_t(1)}){MpIo io;io.page=page;auto driver=qualified(io);OffsetProfilePlan plan;io.transient=1;
        CHECK(driver->preview_offset_profile({25,page,1},{true,false},plan).ok&&io.writes==0);OffsetProfileReceipt receipt;unsigned journals=0;
        CHECK(!driver->apply_offset_profile(plan,{false,false},{}).ok&&io.writes==0);
        CHECK(driver->apply_offset_profile(plan,{true,false},[&](const auto& r){CHECK(io.writes==0);++journals;receipt=r;}).ok);
        CHECK(journals==1&&io.writes==1&&io.word==0xA505&&driver->owned()&&driver->read(false).owned&&!driver->needs_clock_clamp());
        CHECK(driver->verify_offset_profile(plan).ok&&driver->verify_offset_profile(receipt).ok);
        CHECK(!driver->verify({true,false}).ok&&!driver->apply({"offset_mv",30,false},{true,false}).ok&&io.writes==1);
        CHECK(driver->restore().ok&&io.word==0xA502&&!driver->owned());CHECK(driver->verify_offset_profile(receipt,true).ok);
        const auto writes=io.writes;CHECK(driver->restore_offset_profile(receipt).ok&&io.writes==writes);
        CHECK(driver->apply({"offset_mv",30,false},{true,false}).ok);const auto scalar_writes=io.writes;
        CHECK(!driver->restore_offset_profile(receipt).ok&&io.writes==scalar_writes);CHECK(driver->restore().ok&&io.word==0xA502);
    }
    {MpIo io;auto driver=qualified(io);OffsetProfilePlan plan;
        for(double mv:{2.5,5.00000001,-5.00000001,55.0,-55.0,std::numeric_limits<double>::infinity(),std::numeric_limits<double>::quiet_NaN()})CHECK(!driver->preview_offset_profile({mv,0,1},{true,false},plan).ok&&!plan.state&&io.writes==0);
        CHECK(!driver->preview_offset_profile({25,-1,1},{true,false},plan).ok);CHECK(!driver->preview_offset_profile({25,0,0},{true,false},plan).ok);
        CHECK(driver->preview_offset_profile({635,0,1},{true,true},plan).ok);CHECK(driver->preview_offset_profile({-640,0,1},{true,true},plan).ok);
        CHECK(driver->preview_offset_profile({25,0,1},{true,false},plan).ok);io.base=1200;CHECK(!driver->apply_offset_profile(plan,{true,false},{}).ok&&io.writes==0);
    }
    {MpIo io;auto driver=qualified(io);OffsetProfilePlan plan;CHECK(driver->preview_offset_profile({10,0,1},{true,false},plan).ok);unsigned journals=0;
        CHECK(driver->apply_offset_profile(plan,{true,false},[&](const auto&){++journals;}).ok&&journals==0&&!driver->owned()&&io.writes==0);
        io.word=0xA503;CHECK(!driver->verify_offset_profile(plan).ok);
    }
    {MpIo io;auto driver=qualified(io);OffsetProfilePlan plan;CHECK(driver->preview_offset_profile({25,0,1},{true,false},plan).ok);
        io.page=1;CHECK(!driver->apply_offset_profile(plan,{true,false},{}).ok&&io.writes==0);io.page=0;io.scale^=0x400;CHECK(!driver->apply_offset_profile(plan,{true,false},{}).ok&&io.writes==0);
        io.scale^=0x400;io.identity=false;CHECK(!driver->apply_offset_profile(plan,{true,false},{}).ok&&io.writes==0);io.identity=true;io.bound=false;CHECK(!driver->apply_offset_profile(plan,{true,false},{}).ok&&io.writes==0);
    }
    {MpIo io;io.scale=0x8003;auto driver=make_mp29816(io,{0,0x20});CHECK(driver->probe());OffsetProfilePlan plan;
        CHECK(!driver->preview_offset_profile({25,0,0},{true,true},plan).ok&&!driver->read(false).capable&&io.writes==0);
    }
    {MpIo io,other;auto driver=qualified(io),foreign=qualified(other);OffsetProfilePlan plan;CHECK(driver->preview_offset_profile({25,0,1},{true,false},plan).ok);
        CHECK(!foreign->apply_offset_profile(plan,{true,false},{}).ok&&other.writes==0);
        CHECK(!driver->apply_offset_profile({std::make_shared<WrongPlan>()},{true,false},{}).ok&&io.writes==0);
        CHECK(!driver->apply_offset_profile(plan,{true,false},[](const auto&){throw std::runtime_error("Journal failed");}).ok&&io.writes==0&&!driver->owned());
        OffsetProfileReceipt receipt;CHECK(driver->apply_offset_profile(plan,{true,false},[&](const auto& r){receipt=r;}).ok);
        CHECK(!driver->restore_offset_profile({std::make_shared<WrongReceipt>()}).ok&&driver->verify_offset_profile(receipt).ok);
        CHECK(!foreign->restore_offset_profile(receipt).ok&&other.writes==0);CHECK(driver->restore_offset_profile(receipt).ok);
    }
    for(bool throwing:{false,true}){MpIo io;auto driver=qualified(io);OffsetProfilePlan plan;CHECK(driver->preview_offset_profile({25,0,1},{true,false},plan).ok);OffsetProfileReceipt receipt;
        io.fail_at=1;io.throw_after=throwing;const auto result=driver->apply_offset_profile(plan,{true,false},[&](const auto& r){receipt=r;});
        CHECK(!result.ok&&result.may_have_changed&&result.recovery_pending&&driver->owned()&&io.writes==1);
        io.block=false;io.fail_at=0;CHECK(driver->restore_offset_profile(receipt).ok&&io.word==0xA502&&!driver->owned()&&io.writes==2);
    }
    {MpIo io;auto driver=qualified(io);OffsetProfilePlan plan;CHECK(driver->preview_offset_profile({25,0,1},{true,false},plan).ok);OffsetProfileReceipt receipt;
        CHECK(driver->apply_offset_profile(plan,{true,false},[&](const auto& r){receipt=r;}).ok);io.fail_at=2;
        CHECK(!driver->restore_offset_profile(receipt).ok&&io.word==0xA502&&driver->owned());io.fail_at=0;
        CHECK(driver->restore_offset_profile(receipt).ok&&!driver->owned()&&io.writes==2);
    }
    {MpIo io;auto driver=qualified(io);OffsetProfilePlan plan;CHECK(driver->preview_offset_profile({25,0,1},{true,false},plan).ok);OffsetProfileReceipt receipt;
        CHECK(driver->apply_offset_profile(plan,{true,false},[&](const auto& r){receipt=r;}).ok);io.word=0xA506;
        CHECK(!driver->restore_offset_profile(receipt).ok&&io.writes==1);io.word=0xB505;CHECK(!driver->restore_offset_profile(receipt).ok&&io.writes==1);
        io.word=0xA505;io.page=1;CHECK(!driver->probe()&&!driver->restore_offset_profile(receipt).ok&&io.writes==1);io.page=0;
        io.word=0xA502;CHECK(driver->restore_offset_profile(receipt).ok&&!driver->owned()&&io.writes==1);
    }
    std::printf("MP29816 exact coarse profile: %u checks\n",checks);return 0;
}
