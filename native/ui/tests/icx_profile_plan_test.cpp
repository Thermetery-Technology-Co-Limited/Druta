/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "i2c_session.h"
#include "profile_io.h"
#include "icx_preset.h"
#include <windows.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <vector>
#include <stdexcept>
using namespace druta::ui;
#define CHECK(x) do { if(!(x)) { std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x); std::abort(); } } while(false)
namespace {
dc_gpu_identity ident{}; std::map<uint32_t,std::vector<uint8_t>> regs; int writes=0, lock_depth=0; uint32_t clock_hold=0; bool frequency_available=true, mismatch_final=false, after_write=false, uncertain_selected=false; int loadline_reads=0;
int clamp_calls=0,release_calls=0,release_failures=0,status_failures=0,fail_before_write=0;
bool clamp_failure=false,clamp_accept=true,clamp_throw=false,unsettled=false,release_accept_failure=false,release_status_failure=false;
bool b2_failure=false;
bool fail_final_clock_verification=false;int final_clock_fail_after=0;
void setup() {
    ident={}; ident.size=sizeof(ident); ident.nvapi_ok=ident.nvapi_selected=1;ident.nvapi_handle=99;ident.nvapi_devid=0x1234;ident.nvapi_subsys=0x8888;
    std::memcpy(ident.slot,"0000:09:00.0",sizeof("0000:09:00.0"));std::memcpy(ident.uuid,"GPU-test",sizeof("GPU-test"));
    regs={{0xb1,{5,0xfc,0x2d,0x13,0x77,0x42}},{0xb2,{4,0xfc,0,0,0}},{0x19,{9,0,0,0,0xaa,0xbb,0xcc,0xdd,0x04,0x01}},
          {0x1a,{2,4,0xaa}},{0x1b,{2,1,0}},{0x1c,{2,5,6}}};
    writes=lock_depth=0;clock_hold=0;frequency_available=true;mismatch_final=after_write=uncertain_selected=false;loadline_reads=0;
    clamp_calls=release_calls=release_failures=status_failures=fail_before_write=0;clamp_failure=clamp_throw=unsettled=release_accept_failure=release_status_failure=false;clamp_accept=true;
    b2_failure=false;
    fail_final_clock_verification=false;final_clock_fail_after=0;
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
    if(writes==fail_before_write)return DC_E_FAILED;
    if(reg==0xb2) {CHECK(size==5);regs[0xb2][1]=0xfc;if(b2_failure){b2_failure=false;return DC_E_FAILED;}}
    else if(reg==0x10) {CHECK(size==4);regs[0x19][1]=value[1];regs[0x19][2]=value[2];regs[0x19][3]=value[3];}
    else if(reg==0x18) {CHECK(size==4);regs[0x19][8]=value[2];regs[0x19][9]=value[3];}
    else {CHECK(size==2);uint32_t state=reg==0x11?0x1a:(reg==0x12||reg==0x1e?0x1b:0x1c);size_t index=reg==0x1e||reg==0x1f?2:1;regs[state][index]=value[1];}
    if(uncertain_selected && reg!=0xb2){uncertain_selected=false;return DC_E_FAILED;} return DC_OK;
}
dc_status DC_CALL architecture(dc_gpu*,uint32_t* value) {*value=7;return DC_OK;}
dc_status DC_CALL flags(dc_gpu*,int32_t* unrestricted,int32_t* xoc) {*unrestricted=0;*xoc=0;return DC_OK;}
dc_status DC_CALL clocks(dc_gpu*,dc_clock_row* rows,size_t cap,size_t* count) {*count=1;if(!rows||!cap)return DC_E_BUFFER;rows[0].mem_mhz=7000;rows[0].count=1;rows[0].graphics[0]=300;return DC_OK;}
dc_status DC_CALL clamp(dc_gpu*,int64_t low,int64_t high,dc_outcome* out) {CHECK(low==300&&high==300);++clamp_calls;if(clamp_accept)clock_hold=300;if(clamp_throw)throw std::runtime_error("Clamp accepted then transport threw");out->ok=clamp_failure?0:1;return clamp_failure?DC_E_FAILED:DC_OK;}
dc_status DC_CALL release(dc_gpu*,int32_t,dc_outcome* out) {++release_calls;if(release_failures){--release_failures;if(release_accept_failure)clock_hold=0;return DC_E_FAILED;}clock_hold=0;out->ok=1;if(release_status_failure){release_status_failure=false;status_failures=1;}if(fail_final_clock_verification){fail_final_clock_verification=false;final_clock_fail_after=2;}return DC_OK;}
dc_status DC_CALL clock_status(dc_gpu*,dc_clk_lock_status* out) {*out={};if(status_failures){--status_failures;return DC_E_FAILED;}if(final_clock_fail_after&&!--final_clock_fail_after)return DC_E_FAILED;out->locked=clock_hold!=0;out->min_mhz=out->max_mhz=clock_hold;return DC_OK;}
dc_status DC_CALL vf_status(dc_gpu*,int32_t,int64_t,dc_vf_lock_status* out) {*out={};return DC_OK;}
dc_status DC_CALL telemetry(dc_gpu*,dc_telemetry* out) {out->present=DC_T_CORE;out->core=unsettled?500:clock_hold;return DC_OK;}
I2cApi api() {I2cApi a{};a.identity=identity;a.lock=lock;a.unlock=unlock;a.read=read_scalar;a.read_bytes=read_block;a.controller_write=write_block;a.architecture=architecture;a.flags=flags;a.clocks=clocks;a.clamp=clamp;a.release=release;a.clock_status=clock_status;a.vf_status=vf_status;a.telemetry=telemetry;a.sleep=[](unsigned){};return a;}
Command cmd(Command::Operation op,const I2cSession& s) {Command c;c.operation=op;c.i2c_selection_revision=s.data().selection_revision;return c;}
void select(I2cSession& s) {auto c=cmd(Command::Operation::i2c_select,s);c.i2c_family=I2cFamily::icx;c.i2c_route=I2cRoute{1,0x2d};CHECK(s.execute(c).ok);}
void gate(I2cSession& s) {auto c=cmd(Command::Operation::i2c_gate,s);c.enabled=true;CHECK(s.execute(c).ok);}
}
static ProfileJson changed_document(const I2cSession& s){auto d=capture_icx_preset(s.data().controller);d["settings"]["loadline_level"]=5;d["settings"]["fbvdd_request_mv"]=1310;return d;}
static ActionResult apply_receipt(I2cSession& s,const IcxProfilePlan& p,IcxProfileReceipt& receipt){return s.apply_icx_profile(p,[&](const auto& r){receipt=r;});}
int main(){
 {setup();I2cSession s(api(),reinterpret_cast<dc_gpu*>(1),ident);select(s);gate(s);const auto original=capture_icx_preset(s.data().controller);const auto document=changed_document(s);IcxProfilePlan plan;IcxProfileReceipt receipt;
  CHECK(s.preflight_icx_profile(document,plan).ok&&writes==0&&clamp_calls==0&&!s.owned());unsigned journals=0;
  CHECK(s.apply_icx_profile(plan,[&](const auto& r){CHECK(writes==0&&clamp_calls==0&&clock_hold==0);receipt=r;++journals;}).ok&&journals==1&&s.owned()&&clock_hold==300);
  CHECK(s.verify_icx_profile(plan).ok&&s.verify_icx_profile(receipt).ok);const auto applied=writes;auto scalar=cmd(Command::Operation::i2c_apply,s);scalar.i2c_field="loadline";scalar.value=6;CHECK(!s.execute(scalar).ok&&writes==applied);
  CHECK(!s.restore_icx_profile({}).ok&&s.verify_icx_profile(receipt).ok&&writes==applied);
  CHECK(s.restore_icx_profile(receipt).ok&&!s.owned()&&clock_hold==0&&capture_icx_preset(s.data().controller)["settings"]==original["settings"]);
  CHECK(s.verify_icx_profile(receipt,true).ok);const auto restored=writes;const auto released=release_calls;CHECK(s.restore_icx_profile(receipt).ok&&writes==restored&&release_calls==released);
 }
 {setup();I2cSession s(api(),reinterpret_cast<dc_gpu*>(1),ident);select(s);gate(s);auto document=changed_document(s);IcxProfilePlan plan;CHECK(s.preflight_icx_profile(document,plan).ok);regs[0x1a][1]=3;CHECK(!s.apply_icx_profile(plan,{}).ok&&writes==0&&clamp_calls==0);regs[0x1a][1]=4;std::strcpy(ident.uuid,"GPU-other");CHECK(!s.apply_icx_profile(plan,{}).ok&&writes==0&&clamp_calls==0);}
 {setup();I2cSession s(api(),reinterpret_cast<dc_gpu*>(1),ident);select(s);gate(s);auto document=changed_document(s);IcxProfilePlan plan;CHECK(s.preflight_icx_profile(document,plan).ok);const auto rejected=s.apply_icx_profile(plan,[](const auto&){throw std::runtime_error("Host journal rejected");});CHECK(!rejected.ok&&!rejected.may_have_changed&&writes==0&&clamp_calls==0&&!s.owned());}
 {setup();auto a=api();a.clamp=nullptr;a.clocks=nullptr;a.clock_status=nullptr;a.release=nullptr;a.telemetry=nullptr;I2cSession s(a,reinterpret_cast<dc_gpu*>(1),ident);select(s);gate(s);const auto document=capture_icx_preset(s.data().controller);IcxProfilePlan plan;CHECK(s.preflight_icx_profile(document,plan).ok);unsigned journals=0;CHECK(s.apply_icx_profile(plan,[&](const auto&){++journals;}).ok&&journals==0&&writes==0&&clamp_calls==0&&!s.owned()&&s.verify_icx_profile(plan).ok);regs[0x1a][1]=3;CHECK(!s.verify_icx_profile(plan).ok);}
 {setup();I2cSession s(api(),reinterpret_cast<dc_gpu*>(1),ident);select(s);gate(s);auto document=changed_document(s);frequency_available=false;IcxProfilePlan plan;CHECK(!s.preflight_icx_profile(document,plan).ok&&!plan.state&&writes==0&&clamp_calls==0);}
 for(int mode=0;mode<4;++mode){setup();I2cSession s(api(),reinterpret_cast<dc_gpu*>(1),ident);select(s);gate(s);auto document=changed_document(s);IcxProfilePlan plan;IcxProfileReceipt receipt;CHECK(s.preflight_icx_profile(document,plan).ok);clamp_failure=mode!=2;clamp_accept=mode!=0;unsettled=mode==2;clamp_throw=mode==3;
  const auto attempted=apply_receipt(s,plan,receipt);CHECK(!attempted.ok&&attempted.may_have_changed&&receipt.state&&clamp_calls==1&&writes==0&&s.owned());clamp_failure=clamp_throw=false;
  CHECK(s.restore_icx_profile(receipt).ok&&!s.owned()&&clock_hold==0&&writes==0&&release_calls==(mode==0?0:1)&&s.verify_icx_profile(receipt,true).ok);
 }
 {setup();I2cSession s(api(),reinterpret_cast<dc_gpu*>(1),ident);select(s);gate(s);auto document=changed_document(s);IcxProfilePlan plan;IcxProfileReceipt receipt;CHECK(s.preflight_icx_profile(document,plan).ok);uncertain_selected=true;CHECK(!apply_receipt(s,plan,receipt).ok&&s.owned()&&clock_hold==300);CHECK(s.restore_icx_profile(receipt).ok&&!s.owned()&&clock_hold==0);}
 {setup();I2cSession s(api(),reinterpret_cast<dc_gpu*>(1),ident);select(s);gate(s);auto document=changed_document(s);IcxProfilePlan plan;IcxProfileReceipt receipt;CHECK(s.preflight_icx_profile(document,plan).ok&&apply_receipt(s,plan,receipt).ok);fail_before_write=writes+2;CHECK(!s.restore_icx_profile(receipt).ok&&s.owned()&&clock_hold==300);const auto prefix=writes;fail_before_write=0;CHECK(s.restore_icx_profile(receipt).ok&&!s.owned()&&clock_hold==0&&writes==prefix+1&&s.verify_icx_profile(receipt,true).ok);}
 for(int mode=0;mode<3;++mode){setup();I2cSession s(api(),reinterpret_cast<dc_gpu*>(1),ident);select(s);gate(s);auto document=changed_document(s);IcxProfilePlan plan;IcxProfileReceipt receipt;CHECK(s.preflight_icx_profile(document,plan).ok&&apply_receipt(s,plan,receipt).ok);release_failures=mode==2?0:1;release_accept_failure=mode==1;release_status_failure=mode==2;CHECK(!s.restore_icx_profile(receipt).ok&&s.owned());const auto restored=writes;const auto calls=release_calls;CHECK(s.restore_icx_profile(receipt).ok&&!s.owned()&&clock_hold==0&&writes==restored&&release_calls==calls+(mode==0?1:0)&&s.verify_icx_profile(receipt,true).ok);}
 {setup();I2cSession s(api(),reinterpret_cast<dc_gpu*>(1),ident);select(s);gate(s);auto document=changed_document(s);IcxProfilePlan plan;IcxProfileReceipt receipt;CHECK(s.preflight_icx_profile(document,plan).ok&&apply_receipt(s,plan,receipt).ok);clock_hold=600;const auto before=writes;CHECK(!s.restore_icx_profile(receipt).ok&&writes==before&&release_calls==0&&clock_hold==600);clock_hold=300;CHECK(s.restore_icx_profile(receipt).ok);}
 {setup();I2cSession s(api(),reinterpret_cast<dc_gpu*>(1),ident);select(s);gate(s);auto document=changed_document(s);IcxProfilePlan plan;IcxProfileReceipt receipt;CHECK(s.preflight_icx_profile(document,plan).ok&&apply_receipt(s,plan,receipt).ok);regs[0x1a][1]=6;const auto before=writes;CHECK(!s.restore_icx_profile(receipt).ok&&writes==before&&release_calls==0);regs[0x1a][1]=5;CHECK(s.release().ok&&!s.owned()&&s.verify_icx_profile(receipt,true).ok);auto scalar=cmd(Command::Operation::i2c_apply,s);scalar.i2c_field="loadline";scalar.value=5;CHECK(s.execute(scalar).ok&&s.owned());const auto later=writes;const auto released=release_calls;CHECK(!s.restore_icx_profile(receipt).ok&&writes==later&&release_calls==released&&clock_hold==300);CHECK(s.release().ok);}
 {setup();I2cSession s(api(),reinterpret_cast<dc_gpu*>(1),ident);select(s);gate(s);auto document=changed_document(s);IcxProfilePlan plan;IcxProfileReceipt receipt;CHECK(s.preflight_icx_profile(document,plan).ok&&apply_receipt(s,plan,receipt).ok&&s.release().ok);IcxProfilePlan next;CHECK(s.preflight_icx_profile(document,next).ok);const auto writes_before=writes,clamps_before=clamp_calls;const auto rejected=s.apply_icx_profile(next,[](const auto&){throw std::runtime_error("Rejected replacement journal");});CHECK(!rejected.ok&&!rejected.may_have_changed&&writes==writes_before&&clamp_calls==clamps_before&&s.verify_icx_profile(receipt,true).ok);}
 {setup();I2cSession s(api(),reinterpret_cast<dc_gpu*>(1),ident),foreign(api(),reinterpret_cast<dc_gpu*>(1),ident);select(s);gate(s);select(foreign);gate(foreign);auto document=changed_document(s);IcxProfilePlan plan;IcxProfileReceipt receipt;CHECK(s.preflight_icx_profile(document,plan).ok);CHECK(!foreign.apply_icx_profile(plan,{}).ok&&writes==0&&clamp_calls==0);CHECK(apply_receipt(s,plan,receipt).ok);const auto before=writes;CHECK(!foreign.restore_icx_profile(receipt).ok&&writes==before&&release_calls==0);CHECK(s.restore_icx_profile(receipt).ok);}
 {setup();I2cSession s(api(),reinterpret_cast<dc_gpu*>(1),ident);select(s);gate(s);auto document=changed_document(s);IcxProfilePlan plan;IcxProfileReceipt receipt;CHECK(s.preflight_icx_profile(document,plan).ok);clamp_failure=true;CHECK(!apply_receipt(s,plan,receipt).ok&&writes==0);clamp_failure=false;release_failures=1;CHECK(!s.restore_icx_profile(receipt).ok&&s.owned());regs[0x1a][1]=6;const auto calls=release_calls;CHECK(!s.restore_icx_profile(receipt).ok&&release_calls==calls&&clock_hold==300);regs[0x1a][1]=4;CHECK(s.restore_icx_profile(receipt).ok&&!s.owned());}
 {setup();I2cSession s(api(),reinterpret_cast<dc_gpu*>(1),ident);select(s);gate(s);regs[0xb2][1]=0xfe;auto document=changed_document(s);IcxProfilePlan plan;IcxProfileReceipt receipt;CHECK(s.preflight_icx_profile(document,plan).ok);b2_failure=true;const auto attempted=apply_receipt(s,plan,receipt);CHECK(!attempted.ok&&attempted.may_have_changed&&s.owned()&&writes==1&&regs[0xb2][1]==0xfc&&regs[0x1a][1]==4);const auto restored=s.restore_icx_profile(receipt);CHECK(restored.ok&&!s.owned()&&clock_hold==0&&writes==1&&restored.message.find("B2")!=std::string::npos&&regs[0xb2][1]==0xfc);}
 {setup();I2cSession s(api(),reinterpret_cast<dc_gpu*>(1),ident);select(s);gate(s);auto document=changed_document(s);IcxProfilePlan plan;IcxProfileReceipt receipt;CHECK(s.preflight_icx_profile(document,plan).ok&&apply_receipt(s,plan,receipt).ok);CHECK(!s.data().status.recovery_pending);fail_final_clock_verification=true;
  CHECK(!s.restore_icx_profile(receipt).ok&&clock_hold==0&&!s.data().clock_owned&&s.owned()&&s.data().status.owned&&s.data().status.recovery_pending&&!s.data().status.writable);
  const auto restored=writes,released=release_calls,clamped=clamp_calls;auto scalar=cmd(Command::Operation::i2c_apply,s);scalar.i2c_field="loadline";scalar.value=6;CHECK(!s.execute(scalar).ok);
  auto selection=cmd(Command::Operation::i2c_select,s);selection.i2c_family=I2cFamily::icx;selection.i2c_route=I2cRoute{1,0x2d};CHECK(!s.execute(selection).ok);IcxProfilePlan replacement;CHECK(!s.preflight_icx_profile(document,replacement).ok&&!replacement.state);
  CHECK(writes==restored&&release_calls==released&&clamp_calls==clamped);CHECK(s.restore_icx_profile(receipt).ok&&!s.owned()&&!s.data().status.owned&&!s.data().status.recovery_pending&&writes==restored&&release_calls==released&&clamp_calls==clamped&&s.verify_icx_profile(receipt,true).ok);
 }
 CHECK(lock_depth==0);std::printf("iCX opaque session lifecycle tests passed\n");return 0;
}
