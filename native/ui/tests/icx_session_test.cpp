/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "i2c_session.h"
#include "profile_io.h"
#include <windows.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <vector>
using namespace druta::ui;
#define CHECK(x) do { if(!(x)) { std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x); std::abort(); } } while(false)
namespace {
dc_gpu_identity ident{}; std::map<uint32_t,std::vector<uint8_t>> regs; int writes=0, lock_depth=0; uint32_t clock_hold=0; bool frequency_available=true, mismatch_final=false, after_write=false; int loadline_reads=0;
void setup() {
    ident={}; ident.size=sizeof(ident); ident.nvapi_ok=ident.nvapi_selected=1;ident.nvapi_handle=99;ident.nvapi_devid=0x1234;ident.nvapi_subsys=0x8888;
    std::memcpy(ident.slot,"0000:09:00.0",sizeof("0000:09:00.0"));std::memcpy(ident.uuid,"GPU-test",sizeof("GPU-test"));
    regs={{0xb1,{5,0xfc,0x2d,0x13,0x77,0x42}},{0xb2,{4,0xfc,0,0,0}},{0x19,{9,0,0,0,0xaa,0xbb,0xcc,0xdd,0x04,0x01}},
          {0x1a,{2,4,0xaa}},{0x1b,{2,1,0}},{0x1c,{2,5,6}}};
    writes=lock_depth=0;clock_hold=0;frequency_available=true;mismatch_final=after_write=false;loadline_reads=0;
}
dc_status DC_CALL identity(const dc_gpu*,dc_gpu_identity* out) {*out=ident;return DC_OK;}
dc_status DC_CALL lock(dc_gpu*) {++lock_depth;return DC_OK;}
dc_status DC_CALL unlock(dc_gpu*) {--lock_depth;return DC_OK;}
dc_status DC_CALL read_scalar(dc_gpu*,uint32_t,uint32_t,uint32_t,uint32_t,uint32_t*,dc_error*) {return DC_E_UNAVAILABLE;}
dc_status DC_CALL read_block(dc_gpu*,uint32_t port,uint32_t addr,uint32_t reg,uint8_t* out,uint32_t size,dc_error*) {
    if(port!=1||addr!=0x2d||(reg==0x1c&&!frequency_available))return DC_E_UNAVAILABLE;
    if(mismatch_final && after_write && reg==0x1a && ++loadline_reads==2) regs[0x1a][1]=3;
    auto it=regs.find(reg);if(it==regs.end()||it->second.size()!=size)return DC_E_UNAVAILABLE;
    std::memcpy(out,it->second.data(),size);return DC_OK;
}
dc_status DC_CALL write_block(dc_gpu*,uint32_t family,uint32_t port,uint32_t addr,uint32_t reg,const uint8_t* value,uint32_t size,dc_error*) {
    CHECK(family==uint32_t(I2cFamily::icx) && port==1 && addr==0x2d && clock_hold && value); ++writes;after_write=true;
    if(reg==0xb2) {CHECK(size==5);regs[0xb2][1]=0xfc;}
    else if(reg==0x10) {CHECK(size==4);regs[0x19][1]=value[1];regs[0x19][2]=value[2];regs[0x19][3]=value[3];}
    else if(reg==0x18) {CHECK(size==4);regs[0x19][8]=value[2];regs[0x19][9]=value[3];}
    else {CHECK(size==2);uint32_t state=reg==0x11?0x1a:(reg==0x12||reg==0x1e?0x1b:0x1c);size_t index=reg==0x1e||reg==0x1f?2:1;regs[state][index]=value[1];}
    return DC_OK;
}
dc_status DC_CALL architecture(dc_gpu*,uint32_t* value) {*value=7;return DC_OK;}
dc_status DC_CALL flags(dc_gpu*,int32_t* unrestricted,int32_t* xoc) {*unrestricted=0;*xoc=0;return DC_OK;}
dc_status DC_CALL clocks(dc_gpu*,dc_clock_row* rows,size_t cap,size_t* count) {*count=1;if(!rows||!cap)return DC_E_BUFFER;rows[0].mem_mhz=7000;rows[0].count=1;rows[0].graphics[0]=300;return DC_OK;}
dc_status DC_CALL clamp(dc_gpu*,int64_t low,int64_t high,dc_outcome* out) {CHECK(low==300&&high==300);clock_hold=300;out->ok=1;return DC_OK;}
dc_status DC_CALL release(dc_gpu*,int32_t,dc_outcome* out) {clock_hold=0;out->ok=1;return DC_OK;}
dc_status DC_CALL clock_status(dc_gpu*,dc_clk_lock_status* out) {*out={};out->locked=clock_hold!=0;out->min_mhz=out->max_mhz=clock_hold;return DC_OK;}
dc_status DC_CALL vf_status(dc_gpu*,int32_t,int64_t,dc_vf_lock_status* out) {*out={};return DC_OK;}
dc_status DC_CALL telemetry(dc_gpu*,dc_telemetry* out) {out->present=DC_T_CORE;out->core=clock_hold;return DC_OK;}
I2cApi api() {I2cApi a{};a.identity=identity;a.lock=lock;a.unlock=unlock;a.read=read_scalar;a.read_bytes=read_block;a.controller_write=write_block;a.architecture=architecture;a.flags=flags;a.clocks=clocks;a.clamp=clamp;a.release=release;a.clock_status=clock_status;a.vf_status=vf_status;a.telemetry=telemetry;a.sleep=[](unsigned){};return a;}
Command cmd(Command::Operation op,const I2cSession& s) {Command c;c.operation=op;c.i2c_selection_revision=s.data().selection_revision;return c;}
void select(I2cSession& s) {auto c=cmd(Command::Operation::i2c_select,s);c.i2c_family=I2cFamily::icx;c.i2c_route=I2cRoute{1,0x2d};CHECK(s.execute(c).ok);}
void gate(I2cSession& s) {auto c=cmd(Command::Operation::i2c_gate,s);c.enabled=true;CHECK(s.execute(c).ok);}
std::wstring temp_path() {wchar_t dir[MAX_PATH]{},path[MAX_PATH]{};CHECK(GetTempPathW(MAX_PATH,dir)>0);CHECK(GetTempFileNameW(dir,L"icx",0,path)!=0);return path;}
}
int main() {
    setup();auto readonly_api=api();readonly_api.controller_write=nullptr;I2cSession readonly(readonly_api,reinterpret_cast<dc_gpu*>(1),ident);select(readonly);
    CHECK(readonly.execute(cmd(Command::Operation::i2c_verify,readonly)).ok && writes==0 && !readonly.data().gate);
    setup();I2cSession s(api(),reinterpret_cast<dc_gpu*>(1),ident);select(s);CHECK(s.data().captured_preset);
    auto prepare=cmd(Command::Operation::i2c_prepare_profile,s);CHECK(s.execute(prepare).ok);auto revision=s.data().prepared_preset_revision;
    regs[0x1c][1]=6; auto path=temp_path(); auto save=cmd(Command::Operation::i2c_save_profile,s);save.profile_path=path;save.i2c_preset_revision=revision;
    CHECK(s.execute(save).ok && writes==0);auto doc=read_profile(path);CHECK(doc["settings"]["nvvdd_frequency_khz"]==500);
    auto load=cmd(Command::Operation::i2c_load_profile,s);load.profile_path=path;CHECK(s.execute(load).ok && writes==0 && s.data().pending_preset);
    auto stage=cmd(Command::Operation::i2c_stage_captured,s);CHECK(s.execute(stage).ok && writes==0);CHECK(s.data().preset_preview.find("NVVDD 500 kHz")!=std::string::npos);
    gate(s);auto apply=cmd(Command::Operation::i2c_apply_profile,s);CHECK(s.execute(apply).ok);CHECK(writes==1 && regs[0x1c][1]==5 && s.owned());
    CHECK(s.execute(cmd(Command::Operation::i2c_release,s)).ok && regs[0x1c][1]==6 && !s.owned());
    CHECK(s.execute(stage).ok); mismatch_final=true;after_write=false;loadline_reads=0;
    CHECK(!s.execute(apply).ok && s.data().pending_preset && regs[0x1a][1]==3);
    mismatch_final=false;CHECK(s.execute(cmd(Command::Operation::i2c_release,s)).ok);
    auto stale=load;--stale.i2c_selection_revision; // use an actual nonzero old revision after reselection
    auto old_revision=s.data().selection_revision;select(s);stale.i2c_selection_revision=old_revision;int old_writes=writes;
    CHECK(!s.execute(stale).ok && writes==old_writes);DeleteFileW(path.c_str());
    setup();frequency_available=false;I2cSession partial(api(),reinterpret_cast<dc_gpu*>(1),ident);select(partial);CHECK(!partial.data().captured_preset);
    gate(partial);auto change=cmd(Command::Operation::i2c_apply,partial);change.i2c_field="ocp_fbvdd";change.value=1;
    CHECK(partial.execute(change).ok && writes==1);frequency_available=true;
    CHECK(partial.execute(cmd(Command::Operation::i2c_read,partial)).ok && !partial.data().captured_preset);
    CHECK(!partial.execute(cmd(Command::Operation::i2c_stage_captured,partial)).ok);
    CHECK(partial.execute(cmd(Command::Operation::i2c_release,partial)).ok && lock_depth==0);
}
