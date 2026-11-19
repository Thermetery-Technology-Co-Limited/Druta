// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "i2c_session.h"
#include "python_i2c_profile.h"
#include <cstdio>
#include <algorithm>
#include <cstring>
#include <map>
#include <stdexcept>
using namespace druta::ui;
namespace {
unsigned checks=0,failures=0;
#define CHECK(x) do{++checks;if(!(x)){++failures;std::fprintf(stderr,"I2C full capture line %d: %s\n",__LINE__,#x);}}while(false)
struct FullFake {
 dc_gpu_identity identity{};I2cRoute route{3,0x34};std::map<uint8_t,std::vector<uint8_t>> regs;
 unsigned reads=0,writes=0,clamps=0;int depth=0,fail_reads=0;bool change_identity=false;
 FullFake(){identity.size=sizeof(identity);identity.nvapi_ok=identity.nvapi_selected=1;identity.nvapi_handle=0x9876;identity.nvapi_devid=0x1234;identity.nvapi_subsys=0x87654321;std::strcpy(identity.slot,"0000:71:01.1");std::strcpy(identity.uuid,"GPU-full-capture-alternate");}
 void reg(uint8_t at,uint32_t value,size_t n=1){auto& b=regs[at];b.resize(n);for(size_t i=0;i<n;++i)b[i]=static_cast<uint8_t>(value>>(8*i));}
};
FullFake* full=nullptr;
dc_status DC_CALL full_identity(const dc_gpu*,dc_gpu_identity* out){*out=full->identity;return DC_OK;}
dc_status DC_CALL full_lock(dc_gpu*){++full->depth;return DC_OK;}
dc_status DC_CALL full_unlock(dc_gpu*){--full->depth;return DC_OK;}
dc_status DC_CALL full_bytes(dc_gpu*,uint32_t port,uint32_t address,uint32_t reg,uint8_t* out,uint32_t count,dc_error*){++full->reads;if(full->fail_reads>0){--full->fail_reads;return DC_E_UNAVAILABLE;}if(full->change_identity){full->identity.uuid[4]='X';full->change_identity=false;}if(port!=full->route.port||address!=full->route.addr7)return DC_E_UNAVAILABLE;const auto found=full->regs.find(static_cast<uint8_t>(reg));if(found==full->regs.end()||found->second.size()!=count)return DC_E_UNAVAILABLE;std::copy(found->second.begin(),found->second.end(),out);return DC_OK;}
dc_status DC_CALL full_read(dc_gpu* g,uint32_t p,uint32_t a,uint32_t r,uint32_t n,uint32_t* out,dc_error* error){if(n>4)return DC_E_UNAVAILABLE;uint8_t bytes[4]{};const auto status=full_bytes(g,p,a,r,bytes,n,error);if(status!=DC_OK)return status;*out=0;for(uint32_t i=0;i<n;++i)*out|=uint32_t(bytes[i])<<(8*i);return DC_OK;}
dc_status DC_CALL full_write(dc_gpu*,uint32_t,uint32_t,uint32_t,uint32_t,uint32_t,dc_error*){++full->writes;return DC_E_FAILED;}
dc_status DC_CALL full_controller_write(dc_gpu*,uint32_t,uint32_t port,uint32_t address,uint32_t reg,const uint8_t* bytes,uint32_t count,dc_error*){++full->writes;if(port!=full->route.port||address!=full->route.addr7)return DC_E_FAILED;full->regs[static_cast<uint8_t>(reg)].assign(bytes,bytes+count);return DC_OK;}
dc_status DC_CALL full_arch(dc_gpu*,uint32_t* value){*value=7;return DC_OK;}
dc_status DC_CALL full_clamp(dc_gpu*,int64_t,int64_t,dc_outcome*){++full->clamps;return DC_E_FAILED;}
I2cApi full_api(FullFake& fake){full=&fake;I2cApi api;api.identity=full_identity;api.read=full_read;api.read_bytes=full_bytes;api.write=full_write;api.controller_write=full_controller_write;api.lock=full_lock;api.unlock=full_unlock;api.architecture=full_arch;api.clamp=full_clamp;api.sleep=[](unsigned){};return api;}
void select_full(I2cSession& session,I2cFamily family,FullFake& fake,const char* recipe=""){Command cmd;cmd.operation=Command::Operation::i2c_select;cmd.i2c_family=family;cmd.i2c_route=fake.route;cmd.i2c_recipe=recipe;auto result=session.execute(cmd);if(!result.ok)std::fprintf(stderr,"Selection: %s\n",result.message.c_str());CHECK(result.ok);}
void nct(FullFake& f){f.route={1,0x15};f.reg(0x5d,0x39);f.reg(0x5e,0x33);f.reg(5,0xa1);f.reg(1,0x80);f.reg(2,5);f.reg(3,0);}
void up(FullFake& f){f.route={5,0x27};f.reg(0x27,0);f.reg(0x28,43);f.reg(0x0a,0x12);f.reg(0x0b,0x34);f.reg(0x0c,0x5d);f.reg(0x2a,0x40);f.reg(0x39,0x94);f.reg(0x2d,80);f.reg(0x2c,42);}
void ncp(FullFake& f){f.route={4,0x22};f.reg(0x99,0x41);f.reg(0x9a,0x0208,2);f.reg(0x9b,0x77);f.reg(0x20,32);f.reg(0x21,255,2);f.reg(0xd2,0xa5);f.reg(0xd3,0x52);}
void mp(FullFake& f){f.reg(0,0);f.reg(0xbe,0x80|f.route.addr7);f.reg(0x21,120,2);f.reg(0xe1,0x4200,2);f.reg(1,0x80);f.reg(0x45,0,2);f.reg(0x23,0xfe,2);f.reg(0x2a,32,2);f.reg(0x2d,0);f.reg(0x1f,48,2);f.reg(0x24,208,2);f.reg(0x8b,850,2);f.reg(0x8c,0xe020,2);f.reg(0x8d,400,2);f.reg(0x44,0,2);f.reg(0x78,0);}
void m298(FullFake& f,int page){f.route={6,0x32};f.regs[0xad]={4,0x16,0xa8,2,0};f.reg(0,static_cast<uint32_t>(page));f.reg(0x29,0xa400,2);f.reg(0x22,0x56fe,2);f.reg(0x8b,190,2);}
void capture_tests(){
 {FullFake f;I2cSession s(full_api(f),reinterpret_cast<dc_gpu*>(1),f.identity);auto result=s.capture_full_profile();CHECK(result.i2c.is_null()&&result.incomplete.empty()&&f.reads==0&&f.writes==0);}
 {FullFake f;nct(f);I2cSession s(full_api(f),reinterpret_cast<dc_gpu*>(1),f.identity);select_full(s,I2cFamily::nct3933,f);const auto revision=s.data().selection_revision;auto result=s.capture_full_profile();CHECK(result.incomplete.empty()&&!result.i2c.is_null());auto saved=parse_python_nct3933_profile(result.i2c);CHECK(saved.outputs[0]==128&&saved.outputs[1]==5&&saved.outputs[2]==0&&saved.configuration==0xa1&&saved.route.port==1&&!result.requires_xoc);CHECK(s.data().selection_revision==revision&&!s.owned()&&f.writes==0&&f.clamps==0&&f.depth==0);
  f.fail_reads=1;CHECK(s.capture_full_profile().incomplete.empty());f.fail_reads=100;CHECK(s.capture_full_profile().i2c.is_null());f.fail_reads=0;CHECK(!s.capture_full_profile().i2c.is_null());f.change_identity=true;CHECK(s.capture_full_profile().i2c.is_null()&&f.writes==0&&f.depth==0);
 }
 {FullFake f;nct(f);I2cSession s(full_api(f),reinterpret_cast<dc_gpu*>(1),f.identity);select_full(s,I2cFamily::nct3933,f);Command gate;gate.operation=Command::Operation::i2c_gate;gate.enabled=true;CHECK(s.execute(gate).ok);Command apply;apply.operation=Command::Operation::i2c_apply;apply.i2c_field="out1";apply.value=40;CHECK(s.execute(apply).ok&&s.owned());const auto writes=f.writes;auto captured=s.capture_full_profile();CHECK(captured.incomplete.empty()&&parse_python_nct3933_profile(captured.i2c).outputs[0]==0x82&&s.owned()&&f.writes==writes&&f.clamps==0);} {FullFake f;up(f);I2cSession s(full_api(f),reinterpret_cast<dc_gpu*>(1),f.identity);select_full(s,I2cFamily::up9512r,f);auto result=s.capture_full_profile();CHECK(result.incomplete.empty());auto saved=parse_python_up9512r_profile(result.i2c);CHECK((saved.enabled&&saved.offsets_mv==std::array<uint8_t,5>({10,20,30,40,50})));f.reg(0x0a,0x62);CHECK(s.capture_full_profile().requires_xoc);f.reg(0x2a,0);CHECK(!s.capture_full_profile().requires_xoc);f.reg(0x39,0);auto locked=s.capture_full_profile();CHECK(locked.i2c.is_null()&&locked.incomplete.empty());f.regs.erase(0x39);auto missing=s.capture_full_profile();CHECK(missing.i2c.is_null()&&!missing.incomplete.empty()&&f.writes==0&&f.clamps==0&&f.depth==0);}
 {FullFake f;ncp(f);I2cSession s(full_api(f),reinterpret_cast<dc_gpu*>(1),f.identity);select_full(s,I2cFamily::ncp4206,f);auto result=s.capture_full_profile();CHECK(result.incomplete.empty());auto saved=parse_python_ncp4206_profile(result.i2c);CHECK(!saved.enabled&&saved.command==255);f.reg(0x21,44,2);f.reg(0xd2,0xad);f.reg(0xd3,0x5a);CHECK(s.capture_full_profile().requires_xoc);saved=parse_python_ncp4206_profile(s.capture_full_profile().i2c);CHECK(saved.enabled&&saved.command==44&&f.writes==0);f.reg(0xd3,0x52);CHECK(!s.capture_full_profile().incomplete.empty()&&f.depth==0);}
 for(bool manual:{false,true}){FullFake f;mp(f);I2cSession s(full_api(f),reinterpret_cast<dc_gpu*>(1),f.identity);select_full(s,I2cFamily::mp2888,f,manual?"mp2888a-pmbus":"mp2888a-offset");auto result=s.capture_full_profile();CHECK(result.incomplete.empty());const auto saved=parse_python_offset_profile(result.i2c);CHECK(saved.family==I2cFamily::mp2888&&saved.offset_mv==-12.5&&saved.route.port==f.route.port&&f.writes==0&&f.clamps==0);f.reg(0x23,30,2);CHECK(s.capture_full_profile().requires_xoc);f.reg(0xe1,0x4210,2);result=s.capture_full_profile();CHECK(result.i2c.is_null()&&!result.incomplete.empty()&&f.depth==0&&f.writes==0);}
 for(int page:{0,1}){FullFake f;m298(f,page);I2cSession s(full_api(f),reinterpret_cast<dc_gpu*>(1),f.identity);select_full(s,I2cFamily::mp29816,f);auto result=s.capture_full_profile();CHECK(result.incomplete.empty());const auto saved=parse_python_offset_profile(result.i2c);CHECK(saved.family==I2cFamily::mp29816&&saved.page==page&&saved.scale==1&&saved.offset_mv==-10&&f.writes==0&&f.clamps==0&&f.depth==0);f.reg(0x29,0xa000,2);auto drift=s.capture_full_profile();CHECK(drift.i2c.is_null()&&!drift.incomplete.empty());}
 {FullFake f;m298(f,0);f.reg(0x29,0xa000,2);I2cSession s(full_api(f),reinterpret_cast<dc_gpu*>(1),f.identity);select_full(s,I2cFamily::mp29816,f);auto readonly=s.capture_full_profile();CHECK(readonly.i2c.is_null()&&readonly.incomplete.empty()&&f.writes==0);}
 {FullFake f;f.route={1,0x2d};f.regs[0xb1]={5,0xfc,0x2d,0x13,0,0};f.regs[0x19]={9,1,180,0,0,0,0,0,4,1};f.regs[0x1a]={2,0,0};f.regs[0x1b]={2,0,0};f.regs[0x1c]={2,6,6};I2cSession s(full_api(f),reinterpret_cast<dc_gpu*>(1),f.identity);select_full(s,I2cFamily::icx,f);const auto reads=f.reads;auto result=s.capture_full_profile();CHECK(result.i2c.is_null()&&result.incomplete.empty()&&f.reads==reads&&f.writes==0);}
 // Both original-route and discovered recipe identities roundtrip through the strict importer.
 for(const auto route:{I2cRoute{1,0x20},I2cRoute{7,0x35}}){auto doc=python_i2c_profile_identity(I2cFamily::mp2888,route);doc["offset_mv"]=6.25;CHECK(parse_python_offset_profile(doc).route.port==route.port);}
 for(const auto route:{I2cRoute{2,0x30},I2cRoute{4,0x32}}){auto doc=python_i2c_profile_identity(I2cFamily::mp29816,route,0,1);doc["offset_mv"]=5;CHECK(parse_python_offset_profile(doc).scale==1);}
}
}
int main(){capture_tests();std::printf("I2C full capture: %u checks, %u failures\n",checks,failures);return failures?1:0;}
