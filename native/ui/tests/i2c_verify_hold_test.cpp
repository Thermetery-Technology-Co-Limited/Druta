/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "../i2c_verify_hold.h"
#include <cassert>
using namespace druta::ui;
namespace {
uint32_t arch=7, mode=0, uv=12345; int sets=0, recovers=0; bool pending=false, fail_set=false, p0=true, legacy=false, loaded=true, p0_after_set=false;
dc_vf_lock_recovery record{};
dc_status DC_CALL architecture(dc_gpu*,uint32_t* a) {*a=arch;return DC_OK;}
dc_status DC_CALL status(dc_gpu*,int32_t,int64_t,dc_vf_lock_status* s) {*s={};s->locked=mode==3;s->lock={6,mode,uv,1};return DC_OK;}
dc_status DC_CALL clock_status(dc_gpu*,dc_clk_lock_status* s) {*s={};return DC_OK;}
dc_status DC_CALL entry(dc_gpu*,int64_t,dc_vf_point_lock* s) {*s={6,mode,uv,1};return DC_OK;}
dc_status DC_CALL set(dc_gpu*,int64_t value,int32_t,int64_t,dc_outcome* out) {++sets;mode=3;uv=uint32_t(value);if(p0_after_set)p0=true;out->ok=!fail_set;return DC_OK;}
int32_t DC_CALL recovery_pending(const dc_gpu*) {return pending;}
dc_status DC_CALL recovery_get(dc_gpu*,dc_vf_lock_recovery* r) {*r=record;return DC_OK;}
dc_status DC_CALL recovery_set(dc_gpu*,const dc_vf_lock_recovery* r) {record=*r;pending=true;return DC_OK;}
dc_status DC_CALL recover(dc_gpu*,dc_outcome* out) {++recovers;out->ok=false;if(!pending)return DC_OK;
    if((mode==record.requested_mode && uv==record.requested_uv)||(mode==record.previous_mode && uv==record.previous_uv)) {
      mode=record.previous_mode;uv=record.previous_uv;pending=false;out->ok=true;} return DC_OK;}
dc_status DC_CALL curve(dc_gpu*,dc_vf_curve* c) {c->count=3;c->points[0].volt_mv=800;c->points[1].volt_mv=900;c->points[2].volt_mv=1000;return DC_OK;}
dc_status DC_CALL voltage(dc_gpu*,int32_t* has,double* v) {*has=loaded;*v=950;return DC_OK;}
dc_status DC_CALL telemetry(dc_gpu*,dc_telemetry* t) {t->present=DC_T_PSTATE|DC_T_CORE|DC_T_MEM;t->pstate=p0?0:2;t->core=1200;t->mem=4000;return DC_OK;}
dc_status DC_CALL legacy_supported(dc_gpu*,int32_t* out) {*out=1;return DC_OK;}
dc_status DC_CALL legacy_owned(dc_gpu*,int32_t* out) {*out=legacy;return DC_OK;}
dc_status DC_CALL legacy_request(dc_gpu*,int32_t* acquired,dc_outcome* out) {*acquired=!legacy;legacy=true;out->ok=true;return DC_OK;}
dc_status DC_CALL legacy_release(dc_gpu*,dc_outcome* out) {legacy=false;out->ok=true;return DC_OK;}
I2cVerifyHoldApi api() {I2cVerifyHoldApi a; a.architecture=architecture;a.vf_status=status;a.clock_status=clock_status;a.vf_entry=entry;a.vf_set=set;a.vf_pending=recovery_pending;a.vf_recovery_get=recovery_get;a.vf_recovery_set=recovery_set;a.vf_recover=recover;a.curve=curve;a.vcore=voltage;a.telemetry=telemetry;a.legacy_supported=legacy_supported;a.legacy_owned=legacy_owned;a.legacy_request=legacy_request;a.legacy_release=legacy_release;a.bound=[] {return true;};a.sleep=[](unsigned){};return a;}
}
int main() {
    auto gpu=reinterpret_cast<dc_gpu*>(1); std::string reason;
    I2cVerifyHold h(api(),gpu); assert(h.prepare(reason) && h.owned() && sets==1 && uv==900000 && h.check());
    uv=850000; assert(!h.check()); assert(!h.release(reason) && h.owned() && uv==850000);
    uv=900000; assert(h.release(reason) && !h.owned() && mode==0 && uv==12345);
    mode=3;uv=975000; int count=sets; I2cVerifyHold borrowed(api(),gpu);
    assert(borrowed.prepare(reason) && !borrowed.owned() && sets==count); assert(borrowed.release(reason) && mode==3 && uv==975000);
    mode=0;uv=12345; loaded=false; I2cVerifyHold missing(api(),gpu); assert(!missing.prepare(reason) && !missing.owned() && sets==count); loaded=true;
    I2cVerifyHold warmup(api(),gpu); count=sets; assert(!warmup.prepare(reason,true,true) && sets==count);
    fail_set=true; I2cVerifyHold failed(api(),gpu); assert(!failed.prepare(reason) && failed.owned()); assert(failed.release(reason) && mode==0 && uv==12345); fail_set=false;
    p0=false; p0_after_set=true; loaded=false; I2cVerifyHold after_load(api(),gpu); count=sets;
    assert(after_load.prepare(reason,true,true,950) && sets==count+1 && after_load.owned());
    assert(after_load.release(reason) && mode==0 && uv==12345); loaded=true;
    p0=false; p0_after_set=false; I2cVerifyHold no_p0(api(),gpu); count=sets;
    assert(!no_p0.prepare(reason) && no_p0.owned() && sets==count+1);
    assert(no_p0.release(reason) && mode==0 && uv==12345); p0=true;
    arch=2; I2cVerifyHold old(api(),gpu); assert(old.prepare(reason,false) && old.owned() && legacy); assert(old.release(reason) && !legacy);
    legacy=true; I2cVerifyHold reused(api(),gpu); assert(reused.prepare(reason) && !reused.owned()); assert(reused.release(reason) && legacy);
}
