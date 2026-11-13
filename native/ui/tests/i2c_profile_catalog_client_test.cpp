// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#define main established_nct_catalog_fixture_entry
#include "core_client_test.cpp"
#undef main
#include "python_full_profile.h"
#include "profile_catalog.h"
#include <map>
namespace {
std::map<uint32_t,uint32_t> nct_regs;int nct_writes=0,nct_fail=0;bool nct_fail_after=false;
dc_status DC_CALL paired_identity(const dc_gpu* gpu,dc_gpu_identity* out){const auto r=identity(gpu,out);out->nvapi_ok=out->nvapi_selected=1;out->nvapi_handle=77;out->nvapi_devid=0x1234;out->nvapi_subsys=0x5566;return r;}
dc_status DC_CALL nct_read(dc_gpu*,uint32_t port,uint32_t addr,uint32_t reg,uint32_t width,uint32_t* out,dc_error*){if(port!=1||addr!=0x15||width!=1||!nct_regs.count(reg))return DC_E_UNAVAILABLE;*out=nct_regs[reg];return DC_OK;}
dc_status DC_CALL nct_block(dc_gpu* gpu,uint32_t port,uint32_t addr,uint32_t reg,uint8_t* out,uint32_t count,dc_error* error){uint32_t value=0;if(count!=1||nct_read(gpu,port,addr,reg,1,&value,error)!=DC_OK)return DC_E_UNAVAILABLE;*out=static_cast<uint8_t>(value);return DC_OK;}
dc_status DC_CALL nct_write(dc_gpu*,uint32_t family,uint32_t port,uint32_t addr,uint32_t reg,const uint8_t* data,uint32_t size,dc_error*){CHECK(family==static_cast<uint32_t>(I2cFamily::nct3933)&&port==1&&addr==0x15&&reg>=1&&reg<=3&&size==1);++nct_writes;if(nct_fail==int(reg)){nct_fail=0;if(nct_fail_after)nct_regs[reg]=*data;return DC_E_FAILED;}nct_regs[reg]=*data;return DC_OK;}
ProfileJson source(){return {{"schema",2},{"device",{{"uuid","GPU-A"},{"slot","0000:02:00.0"}}},{"incomplete",ProfileJson::array()},{"mem_off_units",80},{"mem_off_true_mhz",40},{"mem_off_scale",2},{"i2c",{{"profile","NCT3933U - three current offsets"},{"sha256","34cd0411ab79509259c3675d28213d87cb9ad3f4d8ad14a69dd5103f9683d90e"},{"port",1},{"addr7",21},{"rail","OUT1/OUT2/OUT3"},{"format","i2c.current_dac_control"},{"display_name","NCT3933U - three current offsets"},{"current_dac_control",{{"outputs",ProfileJson::array({128,5,0})},{"configuration",0}}}}}};}
Command profile(const ProfileJson& doc,bool preflight=false){Command c;c.operation=preflight?Command::Operation::preflight_catalog_profile:Command::Operation::apply_catalog_profile;c.catalog_profile=std::make_shared<const ProfileJson>(doc);return c;}
#if defined(_MSC_VER)
__declspec(noinline)
#endif
void run_nct_catalog(){
 nct_regs={{0x5d,0x39},{0x5e,0x33},{5,0},{1,0},{2,2},{3,0}};nct_writes=0;auto a=api();a.identity=paired_identity;a.lock=lock_gpu;a.unlock=unlock_gpu;a.i2c.identity=paired_identity;a.i2c.lock=lock_gpu;a.i2c.unlock=unlock_gpu;a.i2c.read=nct_read;a.i2c.read_bytes=nct_block;a.i2c.controller_write=nct_write;
 auto client=create_core_backend_for_test(a);std::string error;CHECK(client->open("0000:02:00.0",error));auto doc=source();auto selected=select_python_full_profile(doc,{"i2c:controller"});
 CHECK(!client->execute(profile(selected)).ok&&nct_writes==0); // no implicit selection or gate
 Command select;select.operation=Command::Operation::i2c_select;select.i2c_family=I2cFamily::nct3933;select.i2c_route=I2cRoute{1,0x15};CHECK(client->execute(select).ok);Command gate;gate.operation=Command::Operation::i2c_gate;gate.enabled=true;CHECK(client->execute(gate).ok);Command verify;verify.operation=Command::Operation::i2c_verify;CHECK(client->execute(verify).ok);
 CHECK(client->execute(profile(selected,true)).ok&&nct_writes==0);auto mixed=select_python_full_profile(doc,{"i2c:controller","clock:memory"});CHECK(!client->execute(profile(mixed)).ok&&nct_writes==0&&writes==0);
 CHECK(client->execute(profile(selected)).ok&&nct_regs[1]==128&&nct_regs[2]==5&&nct_writes==2);Command undo;undo.operation=Command::Operation::undo;CHECK(client->execute(undo).ok&&nct_regs[1]==0&&nct_regs[2]==2);
 auto malformed=doc;malformed["i2c"]["current_dac_control"]["outputs"][1]=256;Command invalid=profile(ProfileJson{{"format","druta-python-full-tune-selection"},{"version",1},{"source",malformed},{"targets",ProfileJson::array({"clock:memory"})}});const auto initial=nct_writes;CHECK(!client->execute(invalid).ok&&nct_writes==initial);
 auto unsupported=doc;unsupported["i2c"]={{"profile","unsupported"},{"offset_mv",1}};CHECK(!client->execute(profile(unsupported)).ok&&nct_writes==initial);
 nct_regs[5]=1;CHECK(!client->execute(profile(selected)).ok&&nct_writes==initial);nct_regs[5]=0;
 nct_fail=2;nct_fail_after=true;auto partial=client->execute(profile(selected));CHECK(!partial.ok&&partial.may_have_changed&&client->read(false).undo_available);CHECK(client->execute(undo).ok&&nct_regs[1]==0&&nct_regs[2]==2);
 CHECK(client->execute(profile(selected)).ok);const auto old_writes=writes;CHECK(!client->execute(profile(select_python_full_profile(doc,{"clock:memory"}))).ok&&writes==old_writes);nct_regs[1]=7;const auto prior=nct_writes;CHECK(!client->execute(undo).ok&&nct_writes==prior&&client->read(false).undo_available);nct_regs[1]=128;
 Command release;release.operation=Command::Operation::i2c_release;CHECK(client->execute(release).ok);CHECK(client->execute(undo).ok);
 client->close();
}
}
int main(){run_nct_catalog();std::printf("NCT catalog: %d checks, %d failures\n",checks,failures);return failures?1:0;}
