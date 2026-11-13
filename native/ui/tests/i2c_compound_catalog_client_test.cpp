// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#define main established_compound_catalog_fixture_entry
#include "core_client_test.cpp"
#undef main
#include "python_full_profile.h"
#include <map>
namespace {
bool up_controller=true;std::map<uint32_t,uint32_t> bus;int bus_writes=0;
dc_status DC_CALL paired(const dc_gpu* gpu,dc_gpu_identity* out){const auto result=identity(gpu,out);out->nvapi_ok=out->nvapi_selected=1;out->nvapi_handle=77;out->nvapi_devid=0x1234;out->nvapi_subsys=0x5566;return result;}
dc_status DC_CALL get_bus(dc_gpu*,uint32_t port,uint32_t addr,uint32_t reg,uint32_t width,uint32_t* out,dc_error*){if(port!=2||addr!=(up_controller?0x25u:0x20u)||!bus.count(reg)||width<1||width>2)return DC_E_UNAVAILABLE;*out=bus[reg];return DC_OK;}
dc_status DC_CALL get_bytes(dc_gpu* gpu,uint32_t port,uint32_t addr,uint32_t reg,uint8_t* out,uint32_t count,dc_error* error){uint32_t value=0;if(!out||get_bus(gpu,port,addr,reg,count,&value,error)!=DC_OK)return DC_E_UNAVAILABLE;for(uint32_t i=0;i<count;++i)out[i]=uint8_t(value>>(8*i));return DC_OK;}
dc_status DC_CALL set_bytes(dc_gpu*,uint32_t,uint32_t,uint32_t,uint32_t reg,const uint8_t* data,uint32_t count,dc_error*){++bus_writes;bus[reg]=data[0]|(count==2?uint32_t(data[1])<<8:0);return DC_OK;}
ProfileJson compound(bool up){if(up)return {{"profile","uP9512R offsets (port 2, 0x25; physical rail unassigned)"},{"sha256","deefdb8bd126f54dd6431c3cfb36e063952238e53612340502464620d82255a6"},{"port",2},{"addr7",37},{"rail","Controller output"},{"format","i2c.multi_state_offset"},{"display_name","up"},{"control",{{"kind","up9512r-offset-v1"},{"enabled",false},{"offsets_mv",ProfileJson::array({0,0,0,0,0})}}}};return {{"profile","NCP4206 output (port 2, 0x20; physical rail unassigned" ")"},{"sha256","66ccd56952a7f320da421e7ecdbd96ad868e692bfe173a784ea28529e59eaff0"},{"port",2},{"addr7",32},{"rail","Controller output"},{"display_name","ncp"},{"control",{{"kind","absolute_vid"},{"enabled",true},{"command",80}}}};}
ProfileJson source(bool up){return {{"schema",2},{"device",{{"uuid","GPU-A"},{"slot","0000:02:00.0"}}},{"incomplete",ProfileJson::array()},{"mem_off_units",80},{"mem_off_true_mhz",40},{"mem_off_scale",2},{"i2c",compound(up)}};}
Command action(const ProfileJson& d,bool preview=false){Command c;c.operation=preview?Command::Operation::preflight_catalog_profile:Command::Operation::apply_catalog_profile;c.catalog_profile=std::make_shared<const ProfileJson>(d);return c;}
#if defined(_MSC_VER)
__declspec(noinline)
#endif
void run_compound(bool up){up_controller=up;bus_writes=0;bus=up?std::map<uint32_t,uint32_t>{{0x27,0},{0x28,0x2b},{0x0a,0},{0x0b,0},{0x0c,0x0a},{0x2a,0x10},{0x39,0},{0x2d,80},{0x2c,42}}:std::map<uint32_t,uint32_t>{{0x99,0x41},{0x9a,0x0208},{0x9b,0x77},{0x20,32},{0x21,0x44},{0xd2,0xa5},{0xd3,0x52},{0xd7,0}};
 auto a=api();a.identity=paired;a.lock=lock_gpu;a.unlock=unlock_gpu;a.i2c.identity=paired;a.i2c.lock=lock_gpu;a.i2c.unlock=unlock_gpu;a.i2c.read=get_bus;a.i2c.read_bytes=get_bytes;a.i2c.controller_write=set_bytes;auto client=create_core_backend_for_test(a);std::string error;CHECK(client->open("0000:02:00.0",error));auto doc=source(up);auto only=select_python_full_profile(doc,{"i2c:controller"});Command select;select.operation=Command::Operation::i2c_select;select.i2c_family=up?I2cFamily::up9512r:I2cFamily::ncp4206;select.i2c_route=I2cRoute{2,uint8_t(up?0x25:0x20)};CHECK(client->execute(select).ok);
 if(up){CHECK(client->execute(action(only,true)).ok&&client->execute(action(only)).ok&&bus_writes==0);auto changed=doc;changed["i2c"]["control"]["offsets_mv"][0]=10;CHECK(!client->execute(action(select_python_full_profile(changed,{"i2c:controller"}))).ok&&bus_writes==0);}else CHECK(!client->execute(action(only)).ok&&bus_writes==0);
 auto mixed=select_python_full_profile(doc,{"i2c:controller","clock:memory"});const auto prior=writes;CHECK(!client->execute(action(mixed)).ok&&bus_writes==0&&writes==prior);
 auto bad=doc;if(up)bad["i2c"]["control"]["offsets_mv"][0]=11;else bad["i2c"]["control"]["command"]=1;ProfileJson envelope={{"format","druta-python-full-tune-selection"},{"version",1},{"source",bad},{"targets",ProfileJson::array({"clock:memory"})}};CHECK(!client->execute(action(envelope)).ok&&writes==prior&&bus_writes==0);client->close();}
}
int main(){run_compound(true);run_compound(false);std::printf("I2C compound catalog: %d checks, %d failures\n",checks,failures);return failures?1:0;}
