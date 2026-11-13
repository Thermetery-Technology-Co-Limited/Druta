// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "i2c_driver.h"
#include <cstdio>
#include <cstdlib>
#include <map>
#include <stdexcept>
using namespace druta::ui;
static unsigned nct_checks=0;
#define CHECK(x) do{++nct_checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
struct NctProfileIo final:ControllerIo {
    std::map<uint8_t,uint8_t> registers{{0x5d,0x39},{0x5e,0x33},{5,0xA1},{1,0},{2,0x80},{3,0x83}};
    bool bound=true,throw_after_write=false,block_reads=false;
    unsigned writes=0,fail_at=0,corrupt_at=0,transient=0;
    bool controller_bound()const override{return bound;}
    bool read_bytes(I2cRoute,uint8_t reg,size_t size,std::vector<uint8_t>& out)override{if(block_reads)throw std::runtime_error("Injected capture exception");if(transient){--transient;return false;}if(size!=1||!registers.count(reg))return false;out={registers.at(reg)};return true;}
    bool write_bytes(I2cFamily family,I2cRoute,uint8_t reg,const std::vector<uint8_t>& bytes)override{CHECK(family==I2cFamily::nct3933&&reg>=1&&reg<=3&&bytes.size()==1);++writes;registers[reg]=bytes[0];if(writes==corrupt_at)registers[3]^=1;if(throw_after_write){throw_after_write=false;block_reads=true;throw std::runtime_error("Injected uncertain write");}return writes!=fail_at;}
    std::optional<uint32_t> architecture()const override{return 9;}
    std::optional<double> reference_mv()const override{return {};}
    std::optional<double> core_clock_mhz()const override{return {};}
    void sleep_ms(unsigned)override{}
};
static Nct3933State capture(ControllerDriver& driver){Nct3933State out;CHECK(driver.capture_nct3933(out).ok);return out;}
int main(){
    {NctProfileIo io;auto driver=make_nct3933(io,{6,0x14});io.transient=1;const auto before=capture(*driver);auto desired=before;desired.outputs={0x80,0,0x83};Nct3933Plan plan;CHECK(driver->preview_nct3933(desired,3,plan).ok&&io.writes==0);Nct3933Receipt receipt;unsigned journals=0;
        CHECK(!driver->apply_nct3933(plan,{false,false},{}).ok&&io.writes==0);
        CHECK(driver->apply_nct3933(plan,{true,false},[&](const auto& r){CHECK(io.writes==0);++journals;receipt=r;}).ok&&journals==1&&io.writes==2&&io.registers[1]==0x80&&io.registers[2]==0&&io.registers[5]==0xA1&&driver->owned());
        CHECK(driver->verify_nct3933(plan).ok&&driver->verify_nct3933(receipt).ok);
        CHECK(!driver->apply({"out3",20,false},{true,false}).ok&&io.writes==2);io.registers[3]=0x55;
        CHECK(driver->restore_nct3933(receipt).ok&&io.registers[1]==0&&io.registers[2]==0x80&&io.registers[3]==0x55&&!driver->owned());CHECK(driver->verify_nct3933(receipt,true).ok&&driver->restore_nct3933(receipt).ok);
    }
    {NctProfileIo io;auto driver=make_nct3933(io,{1,0x10});auto desired=capture(*driver);Nct3933Plan plan;CHECK(driver->preview_nct3933(desired,7,plan).ok);unsigned journals=0;CHECK(driver->apply_nct3933(plan,{true,false},[&](const auto&){++journals;}).ok&&journals==0&&io.writes==0&&!driver->owned());io.registers[2]=1;CHECK(!driver->verify_nct3933(plan).ok);}
    {NctProfileIo io;auto driver=make_nct3933(io,{1,0x10});auto desired=capture(*driver);Nct3933Plan plan;desired.configuration^=1;CHECK(!driver->preview_nct3933(desired,7,plan).ok&&!plan.state&&io.writes==0);desired.configuration^=1;CHECK(!driver->preview_nct3933(desired,0,plan).ok&&!driver->preview_nct3933(desired,8,plan).ok);}
    {NctProfileIo io;auto driver=make_nct3933(io,{1,0x10});auto desired=capture(*driver);desired.outputs[0]=0x90;Nct3933Plan plan;CHECK(driver->preview_nct3933(desired,1,plan).ok);io.registers[1]=5;CHECK(!driver->apply_nct3933(plan,{true,false},{}).ok&&io.writes==0);io.registers[1]=0;io.registers[5]^=1;CHECK(!driver->apply_nct3933(plan,{true,false},{}).ok&&io.writes==0);}
    {NctProfileIo io,other;auto driver=make_nct3933(io,{1,0x10}),foreign=make_nct3933(other,{1,0x10});auto desired=capture(*driver);desired.outputs[0]=0x90;Nct3933Plan plan;CHECK(driver->preview_nct3933(desired,1,plan).ok);CHECK(!foreign->apply_nct3933(plan,{true,false},{}).ok&&other.writes==0);io.bound=false;CHECK(!driver->apply_nct3933(plan,{true,false},{}).ok&&io.writes==0);}
    {NctProfileIo io;auto driver=make_nct3933(io,{1,0x10});auto desired=capture(*driver);desired.outputs[0]=0x90;Nct3933Plan plan;CHECK(driver->preview_nct3933(desired,1,plan).ok);CHECK(!driver->apply_nct3933(plan,{true,false},[](const auto&){throw std::runtime_error("No journal");}).ok&&io.writes==0&&!driver->owned());CHECK(driver->apply({"out1",20,false},{true,false}).ok);CHECK(!driver->apply_nct3933(plan,{true,false},{}).ok);CHECK(!driver->preview_nct3933(desired,1,plan).ok);CHECK(driver->restore().ok);}
    for(bool thrown:{false,true}){NctProfileIo io;auto driver=make_nct3933(io,{1,0x10});auto desired=capture(*driver);desired.outputs={0x91,0x92,0x93};Nct3933Plan plan;CHECK(driver->preview_nct3933(desired,7,plan).ok);Nct3933Receipt receipt;io.fail_at=2;io.throw_after_write=thrown;
        const auto result=driver->apply_nct3933(plan,{true,false},[&](const auto& r){receipt=r;});CHECK(!result.ok&&result.may_have_changed&&result.recovery_pending&&driver->owned()&&io.writes==(thrown?1u:2u));CHECK(io.registers[3]==0x83);io.block_reads=false;io.fail_at=0;io.registers[3]=0x44;CHECK(driver->restore().ok&&io.registers[1]==0&&io.registers[2]==0x80&&io.registers[3]==0x44&&!driver->owned()&&driver->verify_nct3933(receipt,true).ok);
    }
    {NctProfileIo io;auto driver=make_nct3933(io,{1,0x10});auto desired=capture(*driver);desired.outputs={0x91,0x92,0x83};Nct3933Plan plan;CHECK(driver->preview_nct3933(desired,3,plan).ok);Nct3933Receipt receipt;CHECK(driver->apply_nct3933(plan,{true,false},[&](const auto& r){receipt=r;}).ok);io.registers[2]=0x99;const auto writes=io.writes;CHECK(!driver->restore_nct3933(receipt).ok&&io.writes==writes&&io.registers[1]==0x91);io.registers[2]=0x92;io.registers[5]^=1;CHECK(!driver->restore_nct3933(receipt).ok&&io.writes==writes);io.registers[5]^=1;CHECK(driver->restore_nct3933(receipt).ok);}
    {NctProfileIo io;auto driver=make_nct3933(io,{1,0x10});auto desired=capture(*driver);desired.outputs={0x91,0x92,0x83};Nct3933Plan plan;CHECK(driver->preview_nct3933(desired,3,plan).ok);Nct3933Receipt receipt;CHECK(driver->apply_nct3933(plan,{true,false},[&](const auto& r){receipt=r;}).ok);io.fail_at=io.writes+1;CHECK(!driver->restore_nct3933(receipt).ok&&driver->owned());io.fail_at=0;CHECK(driver->restore_nct3933(receipt).ok&&driver->verify_nct3933(receipt,true).ok);CHECK(driver->apply({"out1",40,false},{true,false}).ok);const auto writes=io.writes;CHECK(!driver->restore_nct3933(receipt).ok&&io.writes==writes);CHECK(driver->restore().ok);}
    {NctProfileIo io;auto driver=make_nct3933(io,{1,0x10});auto desired=capture(*driver);desired.outputs[0]=0x91;Nct3933Plan plan;CHECK(driver->preview_nct3933(desired,1,plan).ok);Nct3933Receipt receipt;io.corrupt_at=1;CHECK(!driver->apply_nct3933(plan,{true,false},[&](const auto& r){receipt=r;}).ok&&driver->owned());io.corrupt_at=0;CHECK(driver->restore_nct3933(receipt).ok&&io.registers[3]==0x82);}
    std::printf("%u NCT profile checks passed\n",nct_checks);
}
