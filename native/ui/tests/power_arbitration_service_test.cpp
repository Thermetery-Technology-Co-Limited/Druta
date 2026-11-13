/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "power_arbitration_service.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <windows.h>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do { ++checks; if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();} }while(false)
namespace {
dc_gpu_identity current{}; int locked=0,identity_calls=0, mismatch_at=0, table_calls=0, group_calls=0;
int table_fail=0, transport_resets=0, bad_report_mode=0;bool transport_bad=false;bool status_absent=false,malformed_packet=false,bad_geometry=false,empty_mask=false,invalid_report=false,real_mode=false;
dc_status DC_CALL identify(const dc_gpu*,dc_gpu_identity* id){++identity_calls;*id=current;if(mismatch_at && identity_calls>=mismatch_at)std::strcpy(id->uuid,"replaced");return DC_OK;}
dc_status DC_CALL lock(dc_gpu*){++locked;return DC_OK;}
dc_status DC_CALL unlock(dc_gpu*){--locked;return DC_OK;}
uint32_t DC_CALL max_words(){return 100000;}
dc_status DC_CALL reset_transport(dc_gpu*,int32_t kind) {CHECK(locked==1 && kind==DC_TRANSPORT_POWER);++transport_resets;transport_bad=false;return DC_OK;}
dc_status DC_CALL table(dc_gpu*,dc_power_layout* l,dc_error* e){
    CHECK(locked==1 && l->size==sizeof(*l));++table_calls;
    if(table_fail>0){--table_fail;transport_bad=true;}
    if(transport_bad){e->kind=DC_ERR_TRANSIENT;std::strcpy(e->message,"temporary table failure");return DC_E_FAILED;}
    if(real_mode) {l->sizes[0]=20512;l->sizes[1]=397048;l->sizes[2]=13876;
        l->info_base=0xcc;l->info_stride=0x104;l->status_base=0x9c;l->status_stride=0x1730;l->control_base=0x14;l->control_stride=0xc4;}
    else {l->sizes[0]=16;l->sizes[1]=24;l->sizes[2]=20;l->info_base=bad_geometry?9:8;}
    return DC_OK;
}
dc_status DC_CALL get(dc_gpu*,int32_t block,uint32_t* out,uint32_t capacity,uint32_t* count,int32_t* present){
    CHECK(locked==1);++group_calls;*present=((block==DC_CL_TABLE_STATUS && status_absent) || (block==DC_CL_TABLE_CONTROL && empty_mask))?0:1;
    if(!*present){*count=0;return DC_OK;}
    *count=capacity;for(uint32_t i=0;i<capacity;++i)out[i]=0;
    if(block==DC_CL_TABLE_INFO && capacity>1)out[1]=empty_mask?0:1;
    if(real_mode && !empty_mask) {
        if(block==DC_CL_TABLE_INFO) {out[0xcc/4]=0x40;out[0xcc/4+1]=3;}
        else if(block==DC_CL_TABLE_CONTROL) {out[3]=255;out[4]=1;out[0x14/4]=3;out[0x14/4+1]=200000;}
        else {out[1]=1;out[0x9c/4]=3;out[0x9c/4+1]=180000;out[0x9c/4+2]=190000;}
    }
    if(malformed_packet && block==DC_CL_TABLE_CONTROL)--*count;
    return DC_OK;
}
dc_status DC_CALL geometry(const dc_power_layout* l,char* reason,uint32_t size){
    if(l->info_base==9){std::snprintf(reason,size,"unsupported layout");return DC_E_UNRECOGNIZED;}return DC_OK;
}
dc_status DC_CALL decode(const dc_power_layout*,const dc_arbitration_words* info,const dc_arbitration_words* control,
    const dc_arbitration_words* status,dc_arbitration_report* out){
    CHECK(locked==1 && info->present && info->count==4 && (empty_mask ? !control->present : control->present && control->count==5) && out->size==sizeof(*out));
    *out={};out->size=sizeof(*out);out->schema=1;
    if(!status->present && !empty_mask){std::strcpy(out->reason,"missing status");return DC_OK;}
    out->available=1;out->has_mask=1;out->mask=empty_mask?0:1;
    if(!empty_mask){out->policy_count=1;out->policies[0].policy=0;out->policies[0].available=1;
        out->policies[0].requested=200000;out->policies[0].effective=180000;out->policies[0].value=190000;out->policies[0].delta=-20000;
        out->policies[0].has_unit=1;std::strcpy(out->policies[0].unit,"mW");}
    if(invalid_report)out->policy_count=DC_ARBITRATION_POLICIES_MAX+1;
    if(bad_report_mode==1)out->policies[0].policy=7; // not in mask bit 0
    if(bad_report_mode==2)out->available=2;
    if(bad_report_mode==3)out->policies[0].available=2;
    if(bad_report_mode==4)out->policies[0].host.count=DC_ARBITRATION_INPUTS_MAX+1;
    if(bad_report_mode==5)out->mask=3; // two mask bits, only one row
    return DC_OK;
}
}
int wmain(int argc,wchar_t** argv){
    current.nvapi_ok=current.nvapi_selected=1;current.nvapi_handle=19;current.nvapi_devid=44;
    std::strcpy(current.slot,"0000:09:00.0");std::strcpy(current.uuid,"GPU-alternate");
    PowerArbitrationApi api{identify,lock,unlock,max_words,table,get,geometry,decode,reset_transport};
    PowerArbitrationService service(api,reinterpret_cast<dc_gpu*>(1),current);
    CHECK(service.model().freshness==ArbitrationFreshness::unavailable && table_calls==0);
    table_fail=1;auto first=service.refresh();
    CHECK(first.freshness==ArbitrationFreshness::current && first.attempts==2 && table_calls==2 && transport_resets==1 && locked==0);
    CHECK(first.report && first.report->policies[0].policy==0 && first.report->policies[0].requested==200000);
    const auto prior=first.report;status_absent=true;auto partial=service.refresh();
    CHECK(partial.freshness==ArbitrationFreshness::stale && partial.report==prior && partial.attempts==2);
    CHECK(partial.status_observed && !partial.status_present && partial.message.find("STATUS")!=std::string::npos);
    status_absent=false;malformed_packet=true;auto malformed=service.refresh();
    CHECK(malformed.freshness==ArbitrationFreshness::stale && malformed.error_kind==DC_ERR_MALFORMED && malformed.report==prior);
    malformed_packet=false;bad_geometry=true;auto unsupported=service.refresh();
    CHECK(unsupported.freshness==ArbitrationFreshness::stale && unsupported.error_kind==DC_ERR_UNSUPPORTED);
    bad_geometry=false;invalid_report=true;CHECK(service.refresh().error_kind==DC_ERR_MALFORMED);invalid_report=false;
    for(bad_report_mode=1;bad_report_mode<=5;++bad_report_mode)CHECK(service.refresh().error_kind==DC_ERR_MALFORMED);
    bad_report_mode=0;
    empty_mask=true;status_absent=true;auto empty=service.refresh();
    CHECK(empty.freshness==ArbitrationFreshness::current && empty.report->policy_count==0 && empty.status_observed && !empty.status_present);
    status_absent=false;empty_mask=false;CHECK(service.refresh().freshness==ArbitrationFreshness::current);
    mismatch_at=identity_calls+2;auto mismatch=service.refresh();
    CHECK(mismatch.freshness==ArbitrationFreshness::unavailable && mismatch.error_kind==DC_ERR_IDENTITY && !mismatch.report && locked==0);
    mismatch_at=0;auto missing_api=api;missing_api.table_get=nullptr;
    PowerArbitrationService missing(missing_api,reinterpret_cast<dc_gpu*>(1),current);
    CHECK(missing.refresh().error_kind==DC_ERR_UNSUPPORTED);
    if(argc>1) {
        HMODULE module=LoadLibraryExW(argv[1],nullptr,LOAD_WITH_ALTERED_SEARCH_PATH);CHECK(module);
        auto real_geometry=reinterpret_cast<decltype(api.geometry)>(GetProcAddress(module,"dc_power_arbitration_geometry"));
        auto real_decode=reinterpret_cast<decltype(api.decode)>(GetProcAddress(module,"dc_power_arbitration_decode"));
        CHECK(real_geometry && real_decode);
        if(real_geometry && real_decode) {
            real_mode=true;status_absent=false;empty_mask=false;
            auto real_api=api;real_api.geometry=real_geometry;real_api.decode=real_decode;
            PowerArbitrationService real(real_api,reinterpret_cast<dc_gpu*>(1),current);
            auto full=real.refresh();CHECK(full.freshness==ArbitrationFreshness::current && full.report->policy_count==1);
            CHECK(full.report->policies[0].available && full.report->policies[0].requested==200000 && full.report->policies[0].effective==180000);
            status_absent=true;auto missing_status=real.refresh();CHECK(missing_status.freshness==ArbitrationFreshness::stale && missing_status.message.find("STATUS")!=std::string::npos);
            empty_mask=true;auto no_policies=real.refresh();CHECK(no_policies.freshness==ArbitrationFreshness::current && no_policies.report->policy_count==0);
        }
        FreeLibrary(module);
    }
    std::printf("power_arbitration_service_test: %u checks passed\n",checks);
}
