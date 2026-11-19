/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "perf_view_model.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do { ++checks; if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();} }while(false)
namespace {
dc_gpu_identity current{}; int identity_calls=0, identity_fail=0, mismatch_at=0, reads=0; int modes[3]{};
dc_status DC_CALL identify(const dc_gpu*,dc_gpu_identity* out) {
    ++identity_calls; if(identity_fail>0){--identity_fail;return DC_E_UNAVAILABLE;}
    *out=current; if(mismatch_at && identity_calls>=mismatch_at)std::strcpy(out->uuid,"GPU-replaced"); return DC_OK;
}
dc_status DC_CALL read_group(dc_gpu*,const char* name,uint32_t attempts,dc_perf_group_result* out,dc_error* error) {
    ++reads; CHECK(attempts==2 && out->size==sizeof(*out) && out->snapshot.size==sizeof(out->snapshot));
    const int i=std::strcmp(name,"limits")==0?0:std::strcmp(name,"cf_controller")==0?1:2;
    out->attempts=2;
    if(modes[i]==1 || modes[i]==2 || modes[i]==3) {
        out->status=DC_PERF_STATUS_FAILED; error->kind=modes[i]==1?DC_ERR_TRANSIENT:modes[i]==2?DC_ERR_UNSUPPORTED:DC_ERR_IDENTITY;
        std::strcpy(error->message,"fixture failure"); return DC_OK;
    }
    out->has_target=1;std::strcpy(out->slot,current.slot);out->architecture=9;
    auto& s=out->snapshot;std::strcpy(s.group,name);s.capacity=8;s.mask_bytes=1;s.mask[0]=0x80;
    std::strcpy(s.info_sha256,"fixture");s.entry_count=1; s.entries[0].index=7; s.entries[0].info_bytes=2;
    s.entries[0].info_raw[0]=0xde;s.entries[0].info_raw[1]=0xad;
    out->status=DC_PERF_STATUS_READY;
    if(modes[i]==4)s.entry_count=DC_PERF_MAX_ENTRIES+1;
    if(modes[i]==5)std::strcpy(out->slot,"0000:08:00.0");
    if(modes[i]==6){s.entry_count=0;out->status=DC_PERF_STATUS_EMPTY;}
    return DC_OK;
}
}
int main(){
    current={};current.nvapi_ok=current.nvapi_selected=1;current.nvapi_handle=7;current.nvapi_devid=17;
    std::strcpy(current.slot,"0000:09:00.0");std::strcpy(current.uuid,"GPU-alternate");
    PerfViewService service({identify,read_group},reinterpret_cast<dc_gpu*>(1),current);
    CHECK(service.model().groups[0].freshness==PerfFreshness::unavailable && reads==0);
    auto first=service.refresh();CHECK(reads==3 && first.groups[0].freshness==PerfFreshness::current);
    CHECK(first.groups[0].snapshot && first.groups[0].snapshot->entries[0].info_raw[0]==0xde);
    modes[0]=1;modes[1]=2;modes[2]=6;auto partial=service.refresh();
    CHECK(partial.groups[0].freshness==PerfFreshness::stale && partial.groups[0].error_kind==DC_ERR_TRANSIENT);
    CHECK(partial.groups[1].freshness==PerfFreshness::stale && partial.groups[1].error_kind==DC_ERR_UNSUPPORTED);
    CHECK(partial.groups[2].freshness==PerfFreshness::current && partial.groups[2].snapshot->entry_count==0);
    CHECK(partial.groups[0].snapshot->entries[0].info_raw[1]==0xad);
    modes[0]=4;modes[1]=0;modes[2]=0;auto malformed=service.refresh();
    CHECK(malformed.groups[0].freshness==PerfFreshness::stale && malformed.groups[0].error_kind==DC_ERR_MALFORMED);
    CHECK(malformed.groups[1].freshness==PerfFreshness::current);
    identity_fail=2;auto unavailable=service.refresh();CHECK(unavailable.groups[1].freshness==PerfFreshness::stale);
    modes[0]=0;auto recovered=service.refresh();CHECK(recovered.groups[0].freshness==PerfFreshness::current);
    mismatch_at=identity_calls+2;auto replaced=service.refresh();CHECK(replaced.groups[0].freshness==PerfFreshness::unavailable && !replaced.groups[0].snapshot);
    mismatch_at=0;modes[0]=5;auto foreign=service.refresh();CHECK(foreign.groups[1].error_kind==DC_ERR_IDENTITY && !foreign.groups[1].snapshot);
    modes[0]=3;auto rejected=service.refresh();CHECK(rejected.groups[2].error_kind==DC_ERR_IDENTITY && !rejected.groups[2].snapshot);
    PerfViewService absent({identify,nullptr},reinterpret_cast<dc_gpu*>(1),current);
    CHECK(absent.refresh().groups[0].error_kind==DC_ERR_UNSUPPORTED);
    std::printf("perf_view_model_test: %u checks passed\n",checks);
}
