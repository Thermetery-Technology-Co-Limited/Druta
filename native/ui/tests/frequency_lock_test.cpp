// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Fake C ABI only; no DLL, worker, driver or GPU.
#include "frequency_lock.h"
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <stdexcept>
using namespace druta::ui;
namespace {
int failures=0;
void check(bool ok,const char* why){if(!ok){++failures;std::printf("FAIL: %s\n",why);}}
struct Fake {
    dc_gpu_identity id{};
    uint32_t arch=7;bool exports=true,legacy_supported=false,legacy_owned=false;
    bool set_ok=true,reset_ok=true,legacy_hold_ok=true,legacy_release_ok=true,throw_after_set=false;
    bool bad_geometry=false,last_missing=false,mutate_identity_in_caps=false;
    int arch_failures=0,caps_failures=0,table_failures=0,status_failures=0,last_failures=0,legacy_read_failures=0;
    int status_failures_after_set=0,status_failures_after_reset=0;
    int sets=0,resets=0,holds=0,releases=0,depth=0,status_reads=0;
    std::optional<FrequencyRange> range,last;
    std::optional<FrequencyRange> accepted_override;
    Fake(){id.size=sizeof(id);id.nvapi_ok=id.nvml_ok=id.nvapi_selected=id.nvml_selected=1;
        std::snprintf(id.slot,sizeof(id.slot),"0000:0a:01.0");std::snprintf(id.uuid,sizeof(id.uuid),"GPU-alternate");
        id.nvapi_handle=1234;id.nvapi_devid=0x99887766;id.nvml_devid=id.nvapi_devid;}
    dc_gpu* gpu(){return reinterpret_cast<dc_gpu*>(this);}
};
Fake& f(dc_gpu* gpu){return *reinterpret_cast<Fake*>(gpu);}
Fake* exports_fake=nullptr;
dc_status DC_CALL lock(dc_gpu* g){++f(g).depth;return DC_OK;}
dc_status DC_CALL unlock(dc_gpu* g){--f(g).depth;return DC_OK;}
dc_status DC_CALL identity(const dc_gpu* g,dc_gpu_identity* out){*out=reinterpret_cast<const Fake*>(g)->id;return DC_OK;}
dc_status DC_CALL architecture(dc_gpu* g,uint32_t* out){auto& x=f(g);if(x.arch_failures-->0)return DC_E_UNAVAILABLE;*out=x.arch;return DC_OK;}
dc_status DC_CALL exports(const char*,const char*,void** out){static int entry;*out=exports_fake->exports?&entry:nullptr;return DC_OK;}
dc_status DC_CALL caps(dc_gpu* g,dc_capabilities* out){auto& x=f(g);if(x.caps_failures-->0)return DC_E_UNAVAILABLE;
    out->present=DC_C_GFX_MIN|DC_C_GFX_MAX;out->gfx_min=300;out->gfx_max=1500;
    if(x.mutate_identity_in_caps)std::snprintf(x.id.uuid,sizeof(x.id.uuid),"GPU-replacement");return DC_OK;}
dc_status DC_CALL tables(dc_gpu* g,dc_clock_row* out,size_t capacity,size_t* count){auto& x=f(g);
    if(x.table_failures-->0)return DC_E_UNAVAILABLE;*count=2;if(!out)return DC_OK;if(capacity<2)return DC_E_BUFFER;
    out[0]={};out[0].mem_mhz=500;out[0].count=3;out[0].graphics[0]=300;out[0].graphics[1]=600;out[0].graphics[2]=900;
    out[1]={};out[1].mem_mhz=1000;out[1].count=x.bad_geometry?513:5;
    for(uint32_t i=0;i<5;++i)out[1].graphics[i]=(i+1)*300;return DC_OK;}
dc_status DC_CALL current(dc_gpu* g,dc_clk_lock_status* out){auto& x=f(g);++x.status_reads;if(x.status_failures-->0)return DC_E_UNAVAILABLE;
    *out={};if(x.range){out->locked=1;out->min_mhz=x.range->minimum;out->max_mhz=x.range->maximum;}return DC_OK;}
dc_status DC_CALL set(dc_gpu* g,int64_t minimum,int64_t maximum,dc_outcome* out){auto& x=f(g);++x.sets;
    if(x.set_ok){x.range=x.accepted_override?x.accepted_override:std::optional<FrequencyRange>{FrequencyRange{static_cast<uint32_t>(minimum),static_cast<uint32_t>(maximum)}};x.last=x.range;}
    else x.last.reset();x.status_failures=x.status_failures_after_set;
    if(x.throw_after_set)throw std::runtime_error("uncertain SET");out->ok=x.set_ok;return DC_OK;}
dc_status DC_CALL accepted(dc_gpu* g,uint32_t* minimum,uint32_t* maximum){auto& x=f(g);
    if(x.last_failures-->0 || x.last_missing || !x.last)return DC_E_UNAVAILABLE;
    *minimum=x.last->minimum;*maximum=x.last->maximum;return DC_OK;}
dc_status DC_CALL reset(dc_gpu* g,int32_t pascal_noop,dc_outcome* out){auto& x=f(g);++x.resets;
    check(pascal_noop==0,"owned frequency release never substitutes Pascal no-op for readback");
    out->ok=x.reset_ok;if(x.reset_ok){x.range.reset();x.last.reset();}x.status_failures=x.status_failures_after_reset;return DC_OK;}
dc_status DC_CALL legacy_supported(dc_gpu* g,int32_t* out){*out=f(g).legacy_supported;return DC_OK;}
dc_status DC_CALL legacy_owned(dc_gpu* g,int32_t* out){auto& x=f(g);if(x.legacy_read_failures-->0)return DC_E_UNAVAILABLE;*out=x.legacy_owned;return DC_OK;}
dc_status DC_CALL legacy_hold(dc_gpu* g,dc_outcome* out){auto& x=f(g);++x.holds;x.legacy_owned=true;out->ok=x.legacy_hold_ok;return DC_OK;}
dc_status DC_CALL legacy_release(dc_gpu* g,dc_outcome* out){auto& x=f(g);++x.releases;out->ok=x.legacy_release_ok;if(x.legacy_release_ok)x.legacy_owned=false;return DC_OK;}
FrequencyLockApi api(Fake& x){exports_fake=&x;FrequencyLockApi a;
    a.lock=lock;a.unlock=unlock;a.identity=identity;a.architecture=architecture;a.export_address=exports;a.capabilities=caps;
    a.lockable_clocks=tables;a.clock_status=current;a.clocks_lock=set;a.last_lock=accepted;a.clocks_reset=reset;
    a.legacy_supported=legacy_supported;a.legacy_owned=legacy_owned;a.legacy_hold=legacy_hold;a.legacy_release=legacy_release;return a;}
void normal() {
    Fake x;FrequencyLock service(api(x),x.gpu(),x.id);x.arch_failures=x.caps_failures=x.table_failures=x.status_failures=1;
    auto data=service.read();check(data.tables_current && data.nvml_capable && data.current_known && !data.current,"bounded read-only retries recover independent capability/status probes");
    check(data.rows.size()==2 && data.rows[0].graphics_mhz.back()==900 && data.rows[1].graphics_mhz.back()==1500,"per-memory lockable lists retain their distinct current-adapter ceilings");
    auto result=service.apply(355,1499);check(result.ok && x.sets==1 && x.range==std::optional<FrequencyRange>{{300,1200}},"range snaps down on the top-memory table and reads back exactly");
    check(service.data().ownership.confirmed && service.needs_cleanup(),"accepted request records explicit lifecycle ownership");
    x.last_missing=true;service.read();check(!service.data().ownership.confirmed && service.data().ownership.recovery,"missing accepted metadata cannot preserve an earlier confirmed badge");
    x.last_missing=false;service.read();check(service.data().ownership.confirmed && !service.data().ownership.recovery,"read-only exact metadata recovery reconfirms without another SET");
    check(service.lock_max().ok && x.range==std::optional<FrequencyRange>{{1500,1500}},"lock-max uses the selected adapter's reported ceiling");
    result=service.release();check(result.ok && x.resets==1 && !service.needs_cleanup() && !x.range,"owned release confirms unlocked before dropping lifecycle state");
    check(service.release().ok && x.resets==1,"release without an owned request does not reset a device");check(x.depth==0,"GPU lock scope balances");
}
void foreign_and_uncertain() {
    Fake x;FrequencyLock service(api(x),x.gpu(),x.id);x.range=FrequencyRange{600,900};
    check(!service.apply(300,1200).ok && x.sets==0,"foreign request is never overwritten by new acquisition");
    x.range.reset();x.throw_after_set=true;x.status_failures_after_set=3;
    check(!service.apply(300,1200).ok && service.needs_cleanup() && service.data().ownership.recovery,"uncertain SET retains possible ownership before readback");
    x.status_failures=3;check(!service.release().ok && x.resets==0 && service.needs_cleanup(),"unreadable current request never triggers blind reset");
    x.throw_after_set=false;x.status_failures_after_set=0;service.read();check(service.data().ownership.confirmed,"later exact readback recovers an uncertain accepted SET without replay");
    x.reset_ok=false;check(!service.release().ok && service.needs_cleanup(),"failed reset retains exact owned recovery");
    x.reset_ok=true;x.status_failures_after_reset=3;check(!service.release().ok && service.needs_cleanup() && !service.data().current_known,"successful but unreadable reset remains uncertain rather than showing its pre-reset request as current");
    const int resets=x.resets;x.status_failures_after_reset=0;check(service.release().ok && x.resets==resets && !service.needs_cleanup(),"retry observes already-unlocked state instead of repeating an uncertain reset");
    check(service.apply(300,1200).ok,"replacement fixture acquires owned range");x.range=FrequencyRange{600,900};
    const int prior=x.resets;check(service.release().ok && x.resets==prior && x.range==std::optional<FrequencyRange>{{600,900}},"foreign replacement is left intact and old ownership is reconciled");
    check(service.apply(600,900).ok==false && x.sets==2,"reconciled foreign request still blocks reacquisition");
}
void identity_and_capabilities() {
    Fake x;FrequencyLock service(api(x),x.gpu(),x.id);check(service.apply(300,1200).ok,"identity fixture establishes a request");
    std::snprintf(x.id.uuid,sizeof(x.id.uuid),"GPU-other");check(!service.release().ok && x.resets==0 && service.needs_cleanup(),"changed GPU identity cannot release a tracked old adapter request");
    check(service.data().ownership.recovery && !service.data().ownership.confirmed && !service.data().current_known && !service.data().tables_current,"identity uncertainty invalidates current readings while retaining lifecycle state");
    Fake geometry;FrequencyLock g(api(geometry),geometry.gpu(),geometry.id);check(g.read().tables_current,"geometry fixture captures valid table");
    geometry.bad_geometry=true;auto bad=g.read();check(!bad.tables_current && !bad.rows.empty() && !g.apply(300,600).ok && geometry.sets==0,"oversized packet count refuses writes while retaining captured controls");
    geometry.bad_geometry=false;geometry.exports=false;check(!g.read().nvml_capable && geometry.sets==0,"export capability is tested independently of driver version");
    Fake changed;FrequencyLock c(api(changed),changed.gpu(),changed.id);changed.mutate_identity_in_caps=true;
    check(!c.apply(300,600).ok && changed.sets==0,"identity is revalidated after capability probes and before SET");
    Fake legacy;legacy.arch=3;legacy.legacy_supported=true;FrequencyLock l(api(legacy),legacy.gpu(),legacy.id);
    auto data=l.read();check(!data.nvml_capable && data.legacy_available,"Maxwell legacy support does not depend on NVML frequency-table capability");
    check(!l.apply(300,600).ok && legacy.sets==0,"architecturally unsupported NVML lock is not offered merely because exports exist");
}
void legacy_cleanup() {
    Fake x;x.arch=2;x.legacy_supported=true;FrequencyLock service(api(x),x.gpu(),x.id);
    check(service.hold_legacy_p0().ok && service.data().ownership.mode==FrequencyLockMode::legacy_p0 && service.needs_cleanup(),"legacy ABI verifies and records P0 ownership independently");
    x.legacy_read_failures=1;check(service.release().ok && x.releases==1 && !service.needs_cleanup(),"legacy owned-status transient read recovers before release");
    x.legacy_hold_ok=false;check(!service.hold_legacy_p0().ok && service.needs_cleanup() && !service.data().ownership.confirmed,"failed legacy verification retains an unreleased session request");
    x.legacy_release_ok=false;check(!service.release().ok && service.needs_cleanup(),"failed legacy release cannot lose ownership record");
    x.legacy_release_ok=true;check(service.release().ok && !service.needs_cleanup(),"explicit legacy recovery releases only its handle-owned request");
    x.legacy_owned=true;const auto before=x.holds;check(!service.hold_legacy_p0().ok && x.holds==before,"untracked same-handle P0 ownership requires coordinated handover first");
    check(service.release().ok && x.legacy_owned,"service never releases untracked legacy ownership");
}
void changed_setter_table() {
    Fake x;x.accepted_override=FrequencyRange{250,1100};FrequencyLock service(api(x),x.gpu(),x.id);
    auto result=service.apply(300,1200);
    check(!result.ok && service.needs_cleanup() && service.data().ownership.recovery && !service.data().ownership.confirmed,
        "setter resnap outside captured bounds fails qualification without erasing accepted ownership");
    check(service.data().ownership.range==x.accepted_override && x.range==x.accepted_override,
        "authoritative accepted metadata identifies the actual request for owned cleanup");
    service.read();check(service.data().ownership.recovery && !service.data().ownership.confirmed,
        "unexpected accepted range stays in recovery after ordinary read-only refresh");
    check(!service.apply(300,1500).ok && x.sets==1,"unqualified accepted range must be released before another SET");
    check(service.release().ok && !service.needs_cleanup() && x.resets==1,"exact accepted tuple can be safely released despite being outside old bounds");
    Fake unknown;unknown.accepted_override=FrequencyRange{250,1100};unknown.last_missing=true;
    FrequencyLock pending(api(unknown),unknown.gpu(),unknown.id);
    check(!pending.apply(300,1200).ok && pending.needs_cleanup(),"missing metadata plus unrecognized post-SET tuple remains a pending owned dispatch");
    pending.read();check(pending.needs_cleanup() && pending.data().ownership.recovery,"unknown pending dispatch is not discarded as an assumed foreign replacement");
    check(!pending.release().ok && unknown.resets==0 && pending.needs_cleanup(),"unknown tuple cannot authorize a blind cleanup reset");
    unknown.last_missing=false;
    check(pending.release().ok && unknown.resets==1 && !pending.needs_cleanup(),"later accepted metadata recovers precise ownership and permits one explicit release");
}
}
int main(){normal();foreign_and_uncertain();identity_and_capabilities();legacy_cleanup();changed_setter_table();std::printf("Frequency lock: %d failures\n",failures);return failures?1:0;}
