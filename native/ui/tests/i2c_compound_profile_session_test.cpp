// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#define main established_compound_fixture_entry
#include "i2c_session_test.cpp"
#undef main
namespace {
int compound_writes=0,fail_at=0;bool up_mode=false;
dc_status DC_CALL compound_read(dc_gpu*,uint32_t port,uint32_t addr,uint32_t reg,uint8_t* out,uint32_t count,dc_error*){
 if(port!=2||addr!=(up_mode?0x25u:0x20u)||!out||count<1||count>2||!regs.count(reg))return DC_E_UNAVAILABLE;
 uint32_t value=regs[reg];if(up_mode&&reg==0x2d&&(regs[0x2a]&0x40))value=80+(regs[0x0a]>>4);
 if(!up_mode&&reg==0xd7){const double mv=(regs[0xd2]&8)?1600-(int(regs[0x21])-2)*6.25:1000;value=(23u<<11)|uint32_t(std::lround(mv*0.512));}
 for(uint32_t i=0;i<count;++i)out[i]=uint8_t(value>>(8*i));return DC_OK;
}
dc_status DC_CALL compound_write(dc_gpu*,uint32_t family,uint32_t port,uint32_t addr,uint32_t reg,const uint8_t* data,uint32_t count,dc_error*){
 CHECK(port==2&&addr==(up_mode?0x25u:0x20u)&&family==uint32_t(up_mode?I2cFamily::up9512r:I2cFamily::ncp4206)&&data&&count>=1&&count<=2);++compound_writes;uint32_t value=data[0];if(count==2)value|=uint32_t(data[1])<<8;regs[reg]=value;if(compound_writes==fail_at)return DC_E_FAILED;return DC_OK;
}
dc_status DC_CALL compound_telemetry(dc_gpu*,dc_telemetry* t){t->present=DC_T_PSTATE|DC_T_CORE|DC_T_MEM;t->pstate=0;t->core=held?held:1000;t->mem=3000;return DC_OK;}
int legacy_hold=0;
dc_status DC_CALL legacy_arch(dc_gpu*,uint32_t* out){*out=2;return DC_OK;}
dc_status DC_CALL legacy_supported(dc_gpu*,int32_t* out){*out=1;return DC_OK;}
dc_status DC_CALL legacy_owned(dc_gpu*,int32_t* out){*out=legacy_hold;return DC_OK;}
dc_status DC_CALL legacy_acquire(dc_gpu*,int32_t* acquired,dc_outcome* out){legacy_hold=1;*acquired=1;out->ok=1;return DC_OK;}
dc_status DC_CALL legacy_release(dc_gpu*,dc_outcome* out){legacy_hold=0;out->ok=1;return DC_OK;}
ProfileJson up_document(){return {{"profile","uP9512R offsets (port 2, 0x25; physical rail unassigned)"},{"sha256","deefdb8bd126f54dd6431c3cfb36e063952238e53612340502464620d82255a6"},{"port",2},{"addr7",37},{"rail","Controller output"},{"format","i2c.multi_state_offset"},{"display_name","Up"},{"control",{{"kind","up9512r-offset-v1"},{"enabled",true},{"offsets_mv",ProfileJson::array({10,20,30,40,50})}}}};}
ProfileJson ncp_document(){return {{"profile","NCP4206 output (port 2, 0x20; physical rail unassigned)"},{"sha256","66ccd56952a7f320da421e7ecdbd96ad868e692bfe173a784ea28529e59eaff0"},{"port",2},{"addr7",32},{"rail","Controller output"},{"display_name","NCP"},{"control",{{"kind","absolute_vid"},{"enabled",true},{"command",90}}}};}
void one(bool up){
 setup();up_mode=up;compound_writes=fail_at=0;regs=up?std::map<int,uint32_t>{{0x27,0},{0x28,0x2b},{0x0a,0},{0x0b,0},{0x0c,0x0a},{0x2a,0x10},{0x39,0x94},{0x2d,80},{0x2c,42}}:std::map<int,uint32_t>{{0x99,0x41},{0x9a,0x0208},{0x9b,0x77},{0x20,32},{0x21,0x44},{0xd2,0xa5},{0xd3,0x52},{0xd7,0}};
 auto a=api();a.read_bytes=compound_read;a.controller_write=compound_write;a.telemetry=compound_telemetry;a.architecture=icx_arch;a.verify_hold.architecture=legacy_arch;a.verify_hold.telemetry=compound_telemetry;a.verify_hold.legacy_supported=legacy_supported;a.verify_hold.legacy_owned=legacy_owned;a.verify_hold.legacy_request=legacy_acquire;a.verify_hold.legacy_release=legacy_release;legacy_hold=0;I2cSession s(a,reinterpret_cast<dc_gpu*>(1),identity);auto doc=up?up_document():ncp_document();
 I2cProfilePlan plan;CHECK(!s.preflight_i2c_profile(doc,plan).ok&&compound_writes==0);auto select=command(Command::Operation::i2c_select);select.i2c_family=up?I2cFamily::up9512r:I2cFamily::ncp4206;select.i2c_route=I2cRoute{2,uint8_t(up?0x25:0x20)};CHECK(s.execute(select).ok);
 auto gate=command(Command::Operation::i2c_gate);gate.enabled=true;
 if(up){auto same=doc;same["control"]["offsets_mv"]=ProfileJson::array({0,0,0,0,0});same["control"]["enabled"]=false;regs[0x39]=0;CHECK(s.preflight_i2c_profile(same,plan).ok);int calls=0;CHECK(s.apply_i2c_profile(plan,[&](const auto&){++calls;}).ok&&calls==0&&compound_writes==0&&!s.owned());regs[0x39]=0x94;}
 CHECK(s.execute(gate).ok);
 auto bad=doc;bad["port"]=1;CHECK(!s.preflight_i2c_profile(bad,plan).ok&&compound_writes==0);
 CHECK(s.execute(command(Command::Operation::i2c_verify)).ok);compound_writes=0;CHECK(s.preflight_i2c_profile(doc,plan).ok);
 identity.nvapi_devid++;const auto prior=compound_writes;CHECK(!s.apply_i2c_profile(plan,{}).ok&&compound_writes==prior);identity.nvapi_devid--;CHECK(s.preflight_i2c_profile(doc,plan).ok);
 I2cProfileReceipt receipt;int journal=0;auto applied=s.apply_i2c_profile(plan,[&](const auto& r){if(!journal)CHECK(compound_writes==0);receipt=r;++journal;});CHECK(applied.ok&&journal>0&&s.owned());CHECK(s.restore_i2c_profile(receipt).ok&&!s.owned());
 CHECK(s.preflight_i2c_profile(doc,plan).ok);auto stale=plan;CHECK(s.execute(select).ok);const auto before=compound_writes;CHECK(!s.apply_i2c_profile(stale,{}).ok&&compound_writes==before);
 CHECK(s.execute(command(Command::Operation::i2c_verify)).ok);CHECK(s.preflight_i2c_profile(doc,plan).ok);fail_at=compound_writes+2;journal=0;applied=s.apply_i2c_profile(plan,[&](const auto& r){receipt=r;++journal;});CHECK(!applied.ok&&applied.may_have_changed&&journal>0);fail_at=0;CHECK(s.restore_i2c_profile(receipt).ok&&!s.owned());
}
}
int main(){one(true);one(false);std::printf("I2C compound profile session: %u checks\n",checks);return 0;}
