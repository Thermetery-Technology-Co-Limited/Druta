// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "i2c_driver.h"
#include <cstdio>
#include <cstdlib>
#include <map>
#include <stdexcept>
#include <limits>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
struct IcxIo final:ControllerIo {
    std::map<uint8_t,std::vector<uint8_t>> packets{{0xb1,{5,0xfc,0x2d,0x13,0xaa,0xbb}},{0xb2,{4,0xfe,1,2,3}},
        {0x19,{9,0,0,0,0xa1,0xb2,0xc3,0xdd,4,1}},{0x1a,{2,5,0xe1}},{0x1b,{2,1,0}},{0x1c,{2,6,5}}};
    bool bound=true,block=false,throw_after=false,retain_auto=false;unsigned writes=0,fail_at=0,transient=0,corrupt_at=0;std::vector<uint8_t> commands;
    bool controller_bound()const override{return bound;}
    bool read_bytes(I2cRoute route,uint8_t reg,size_t count,std::vector<uint8_t>& out)override{
        CHECK(route.port==1&&route.addr7==0x2d);if(block)throw std::runtime_error("Injected read failure");if(transient){--transient;return false;}
        if(!packets.count(reg)||packets[reg].size()!=count)return false;out=packets[reg];return true;
    }
    bool write_bytes(I2cFamily family,I2cRoute route,uint8_t reg,const std::vector<uint8_t>& bytes)override{
        CHECK(family==I2cFamily::icx&&route.port==1&&route.addr7==0x2d);++writes;commands.push_back(reg);
        if(reg==0xb2){CHECK(bytes==std::vector<uint8_t>({4,0xc6,0xeb,0xea,0x15}));packets[0xb2][1]=0xfc;}
        else if(reg==0x10||reg==0x18){CHECK(bytes.size()==4&&bytes[0]==3);auto& p=packets[0x19];const size_t n=reg==0x10?1u:7u;if(n==1)p[n]=bytes[1];else p[n]=uint8_t(p[n]+17);p[n+1]=bytes[2];p[n+2]=bytes[3];if(n==1&&bytes[1]==0&&retain_auto){p[2]=0x34;p[3]=0x12;}}
        else{CHECK(bytes.size()==2&&bytes[0]==1);uint8_t state=0,index=0;if(reg==0x11){state=0x1a;index=1;}else if(reg==0x12||reg==0x1e){state=0x1b;index=reg==0x12?1:2;}else if(reg==0x13||reg==0x1f){state=0x1c;index=reg==0x13?1:2;}else CHECK(false);packets[state][index]=bytes[1];}
        if(writes==corrupt_at)packets[0x19][4]^=1;
        if(throw_after){throw_after=false;block=true;throw std::runtime_error("Accepted write then transport failed");}return writes!=fail_at;
    }
    std::optional<uint32_t> architecture()const override{return 7;}
    std::optional<double> reference_mv()const override{return {};}
    std::optional<double> core_clock_mhz()const override{return 333.0;}
    void sleep_ms(unsigned)override{}
};
static std::vector<ControllerRequest> desired(){return {{"nvvdd",950,false},{"fbvdd",1350,false},{"frequency_nvvdd",700,false},{"frequency_fbvdd",800,false},{"ocp_nvvdd",0,false},{"ocp_fbvdd",1,false},{"loadline",7,false}};}
static std::vector<ControllerRequest> initial(){return {{"nvvdd",0,true},{"fbvdd",1300,false},{"frequency_nvvdd",600,false},{"frequency_fbvdd",500,false},{"ocp_nvvdd",1,false},{"ocp_fbvdd",0,false},{"loadline",5,false}};}
static std::unique_ptr<ControllerDriver> driver(IcxIo& io){auto result=make_icx(io,{1,0x2d});CHECK(result->probe()&&result->verify({true,false}).ok);return result;}
static IcxControllerPlan preview(ControllerDriver& d,const std::vector<ControllerRequest>& requests=desired()){IcxControllerPlan p;const auto r=d.preview_icx(requests,{true,false},p);if(!r.ok)std::fprintf(stderr,"Preview: %s\n",r.message.c_str());CHECK(r.ok);return p;}
static IcxControllerReceipt apply(ControllerDriver& d,const IcxControllerPlan& plan,IcxIo& io){IcxControllerReceipt receipt;const auto start=io.writes;unsigned journals=0;
    const auto result=d.apply_icx(plan,{true,false},[&](const auto& r){CHECK(io.writes==start);receipt=r;++journals;});if(!result.ok)std::fprintf(stderr,"Apply: %s\n",result.message.c_str());CHECK(result.ok&&journals==1);return receipt;}
int main(){
    {IcxIo io;auto d=driver(io);const auto plan=preview(*d);CHECK(io.writes==0);auto receipt=apply(*d,plan,io);
        CHECK(io.commands==std::vector<uint8_t>({0xb2,0x1e,0x18,0x13,0x1f,0x11,0x10,0x12}));CHECK(d->owned()&&d->read(false).owned&&d->verify_icx(plan).ok&&d->verify_icx(receipt).ok);
        CHECK(io.packets[0x19][7]==0xee&&io.packets[0x19][4]==0xa1&&io.packets[0x1a][2]==0xe1);CHECK(!d->apply({"loadline",8,false},{true,false}).ok);
        const auto count=io.commands.size();CHECK(d->restore_icx(receipt).ok&&!d->owned());CHECK(std::vector<uint8_t>(io.commands.begin()+count,io.commands.end())==std::vector<uint8_t>({0x10,0x12,0x18,0x13,0x1f,0x11,0x1e}));
        CHECK(io.packets[0x19][7]==0xff&&d->verify_icx(receipt,true).ok&&io.packets[0xb2][1]==0xfc);const auto writes=io.writes;CHECK(d->restore_icx(receipt).ok&&io.writes==writes);
    }
    {IcxIo io;auto d=driver(io);auto plan=preview(*d,initial());unsigned journals=0;CHECK(d->apply_icx(plan,{true,false},[&](const auto&){++journals;}).ok&&journals==0&&io.writes==0&&!d->owned());
        CHECK(d->verify_icx(plan).ok);io.packets[0x1a][2]^=1;CHECK(!d->verify_icx(plan).ok);
    }
    {IcxIo io;io.retain_auto=true;io.packets[0x19][2]=0x78;io.packets[0x19][3]=0x56;auto d=driver(io);
        auto matching=preview(*d,initial());io.packets[0x19][2]=0x9a;unsigned journals=0;
        CHECK(d->apply_icx(matching,{true,false},[&](const auto&){++journals;}).ok&&journals==0&&io.writes==0&&d->verify_icx(matching).ok);
        auto manual=initial();manual[0]={"nvvdd",950,false};auto receipt=apply(*d,preview(*d,manual),io);
        CHECK(d->restore_icx(receipt).ok&&io.packets[0x19][1]==0&&io.packets[0x19][2]==0x34&&io.packets[0x19][3]==0x12&&d->verify_icx(receipt,true).ok);
        CHECK(d->apply({"nvvdd",950,false},{true,false}).ok); // Establish a manual baseline, then release to Auto.
        CHECK(d->restore().ok);io.packets[0x19][1]=1;io.packets[0x19][2]=190;io.packets[0x19][3]=0;
        auto automatic=preview(*d,initial());auto auto_receipt=apply(*d,automatic,io);
        CHECK(d->verify_icx(automatic).ok&&d->verify_icx(auto_receipt).ok&&io.packets[0x19][2]==0x34);
        CHECK(d->restore_icx(auto_receipt).ok&&io.packets[0x19][1]==1&&io.packets[0x19][2]==190&&d->verify_icx(auto_receipt,true).ok);
    }
    {IcxIo io;auto d=driver(io);for(int variant=0;variant<5;++variant){auto r=desired();if(variant==0)r.pop_back();if(variant==1)r[1]=r[0];if(variant==2)r[0].value=951;if(variant==3)r[0].value=std::numeric_limits<double>::quiet_NaN();if(variant==4)r[1].reset=true;IcxControllerPlan p;CHECK(!d->preview_icx(r,{true,false},p).ok&&!p.state&&io.writes==0);}
        auto r=desired();r[0].value=1500;IcxControllerPlan p;CHECK(!d->preview_icx(r,{true,false},p).ok&&d->preview_icx(r,{true,true},p).ok);CHECK(!d->apply_icx(p,{true,false},{}).ok&&io.writes==0);
        p=preview(*d);CHECK(!d->apply_icx(p,{false,false},{}).ok&&io.writes==0);io.packets[0x19][4]^=1;CHECK(!d->apply_icx(p,{true,false},{}).ok&&io.writes==0);
    }
    {IcxIo io;auto d=driver(io);auto p=preview(*d);const auto r=d->apply_icx(p,{true,false},[](const auto&){throw std::runtime_error("Journal refused");});CHECK(!r.ok&&!r.may_have_changed&&!d->owned()&&io.writes==0);CHECK(d->apply_icx(p,{true,false},{}).ok&&d->restore().ok);}
    for(unsigned fail=1;fail<=8;++fail){IcxIo io;auto d=driver(io);const auto p=preview(*d);IcxControllerReceipt receipt;io.fail_at=fail;
        const auto r=d->apply_icx(p,{true,false},[&](const auto& saved){receipt=saved;});CHECK(!r.ok&&r.may_have_changed&&r.recovery_pending&&d->owned()&&io.writes==fail);
        io.fail_at=0;const auto restored=d->restore_icx(receipt);if(!restored.ok)std::fprintf(stderr,"Restore failure %u: %s\n",fail,restored.message.c_str());CHECK(restored.ok&&!d->owned()&&d->verify_icx(receipt,true).ok);
    }
    {IcxIo io;auto d=driver(io);auto receipt=apply(*d,preview(*d),io);const auto start=io.writes;io.fail_at=start+3;
        CHECK(!d->restore_icx(receipt).ok&&d->owned());const auto retry=io.writes;io.fail_at=0;CHECK(d->restore_icx(receipt).ok&&!d->owned()&&io.writes==retry+4&&d->verify_icx(receipt,true).ok);
    }
    {IcxIo io;auto d=driver(io);auto p=preview(*d);IcxControllerReceipt receipt;io.throw_after=true;
        CHECK(!d->apply_icx(p,{true,false},[&](const auto& r){receipt=r;}).ok&&d->owned()&&io.writes==1);io.block=false;CHECK(d->restore_icx(receipt).ok&&!d->owned()&&io.writes==1);
    }
    {IcxIo io;auto d=driver(io);auto receipt=apply(*d,preview(*d),io);const auto writes=io.writes;io.packets[0x1a][1]=9;CHECK(!d->restore_icx(receipt).ok&&io.writes==writes);io.packets[0x1a][1]=7;
        io.packets[0x19][7]^=1;CHECK(!d->restore_icx(receipt).ok&&io.writes==writes);io.packets[0x19][7]^=1;io.packets[0x19][4]^=1;CHECK(!d->restore_icx(receipt).ok&&io.writes==writes);io.packets[0x19][4]^=1;
        CHECK(d->restore_icx(receipt).ok);CHECK(d->apply({"nvvdd",950,false},{true,false}).ok);const auto scalar=io.writes;CHECK(!d->restore_icx(receipt).ok&&io.writes==scalar&&d->owned());CHECK(d->restore().ok);
    }
    {IcxIo io,other;auto d=driver(io),foreign=driver(other);const auto p=preview(*d);CHECK(!foreign->apply_icx(p,{true,false},{}).ok&&other.writes==0);auto receipt=apply(*d,p,io);CHECK(!foreign->restore_icx(receipt).ok&&other.writes==0);
        io.packets[0xb1][5]^=1;const auto writes=io.writes;CHECK(!d->restore_icx(receipt).ok&&io.writes==writes);io.packets[0xb1][5]^=1;io.transient=1;CHECK(d->restore_icx(receipt).ok);
    }
    {IcxIo io;auto d=driver(io);auto p=preview(*d);IcxControllerReceipt receipt;io.corrupt_at=3;CHECK(!d->apply_icx(p,{true,false},[&](const auto& r){receipt=r;}).ok&&io.writes==3);const auto writes=io.writes;
        CHECK(!d->restore_icx(receipt).ok&&io.writes==writes);io.packets[0x19][4]^=1;io.corrupt_at=0;CHECK(d->restore_icx(receipt).ok&&!d->owned());
    }
    std::printf("iCX typed profile: %u checks\n",checks);return 0;
}
