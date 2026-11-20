// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#define main established_offset_session_fixture_entry
#include "i2c_session_test.cpp"
#undef main
namespace {
dc_status DC_CALL mp_block(dc_gpu*,uint32_t port,uint32_t addr,uint32_t reg,uint8_t* out,uint32_t size,dc_error*){
 if(port!=wanted_port||addr!=wanted_addr||!out)return DC_E_UNAVAILABLE;
 if(reg==0xad&&size==5){const uint8_t id[]={4,0x16,0xa8,2,0};std::copy(id,id+5,out);return DC_OK;}
 if(!regs.count(reg)||size>2)return DC_E_UNAVAILABLE;const auto value=regs[reg];for(uint32_t i=0;i<size;++i)out[i]=uint8_t(value>>(8*i));return DC_OK;
}
dc_status DC_CALL no_write(dc_gpu*,uint32_t,uint32_t,uint32_t,uint32_t,const uint8_t*,uint32_t,dc_error*){++writes;return DC_E_FAILED;}
ProfileJson mp2888(){return {{"profile","RTX 2080 Ti - NVVDD (MP2888A)"},{"sha256","274a1c4d75659db1598d9a58be2e897f120c5ac48a6aba7b40ea22455d3b7837"},{"port",3},{"addr7",42},{"rail","NVVDD"},{"display_name","offset"},{"offset_mv",6.25}};}
ProfileJson mp29816(){return {{"profile","MP29816 PAGE 1 output"},{"sha256","5923399c9ba6043ee48c813b99e22287ad3be72659f5711e411aa2eef587c6cc"},{"port",3},{"addr7",49},{"rail","PAGE 1 output"},{"display_name","offset"},{"offset_mv",10}};}
void tests(){
 setup();wanted_port=3;wanted_addr=42;regs[0xbe]=0xaa;auto a=api();a.read_bytes=mp_block;a.controller_write=no_write;I2cSession s(a,reinterpret_cast<dc_gpu*>(1),identity);I2cProfilePlan plan;auto doc=mp2888();CHECK(!s.preflight_i2c_profile(doc,plan).ok&&writes==0);
 auto select=command(Command::Operation::i2c_select);select.i2c_family=I2cFamily::mp2888;select.i2c_route=I2cRoute{3,42};select.i2c_recipe="mp2888a-pmbus";CHECK(s.execute(select).ok);auto gate=command(Command::Operation::i2c_gate);gate.enabled=true;CHECK(s.execute(gate).ok);CHECK(!s.preflight_i2c_profile(doc,plan).ok&&s.data().control_profile=="mp2888a-pmbus"&&writes==0);
 select.i2c_recipe="mp2888a-offset";CHECK(s.execute(select).ok);CHECK(!s.preflight_i2c_profile(doc,plan).ok&&writes==0); // selected but no loaded-response Verify
 auto bad=doc;bad["port"]=2;CHECK(!s.preflight_i2c_profile(bad,plan).ok&&writes==0);bad=doc;bad["sha256"]="unknown";CHECK(!s.preflight_i2c_profile(bad,plan).ok&&writes==0);
 setup();wanted_port=3;wanted_addr=49;regs={{0,1},{0x29,0x0400},{0x22,0},{0x8b,200}};a=api();a.read_bytes=mp_block;a.controller_write=no_write;I2cSession t(a,reinterpret_cast<dc_gpu*>(1),identity);select=command(Command::Operation::i2c_select);select.i2c_family=I2cFamily::mp29816;select.i2c_route=I2cRoute{3,49};CHECK(t.execute(select).ok);CHECK(t.execute(gate).ok);doc=mp29816();CHECK(!t.preflight_i2c_profile(doc,plan).ok&&writes==0);regs[0]=0;CHECK(!t.preflight_i2c_profile(doc,plan).ok&&writes==0);regs[0]=1;regs[0x29]=0;CHECK(!t.preflight_i2c_profile(doc,plan).ok&&writes==0);
}
}
int main(){tests();std::printf("I2C offset session: %u checks\n",checks);return 0;}
