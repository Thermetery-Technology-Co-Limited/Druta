// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Trace expectations match released Python realclock.ClockEvidence policy.
#include "clock_monitor_service.h"
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
using namespace druta::ui;
static unsigned checks=0,calls=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
namespace {
dc_clock_domains response{};dc_status result=DC_OK;
dc_status DC_CALL classify(const uint32_t* words,size_t count,int32_t,uint32_t,int32_t,uint32_t,int32_t,uint32_t,dc_clock_domains* out){
    CHECK(words!=nullptr&&count==288);++calls;*out=response;return result;
}
dc_clock_domain_row domain(uint32_t id,const char* name,uint32_t a,uint32_t b,uint32_t scale=1,const char* grade="confirmed"){
    dc_clock_domain_row row{};row.domain=id;std::snprintf(row.name,sizeof(row.name),"%s",name);
    std::snprintf(row.kind,sizeof(row.kind),"%s","freq");std::snprintf(row.grade,sizeof(row.grade),"%s",grade);
    row.prog_khz=a;row.meas_khz=b;row.scale=scale;row.has_prog_mhz=row.has_meas_mhz=1;row.has_delta_mhz=a&&b;
    row.prog_mhz=a/1000.0;row.meas_mhz=b/1000.0;row.delta_mhz=(static_cast<double>(b)-a)/1000;return row;
}
struct Feeder {
    ClockMonitorService service;ClockMonitorInput input;dc_telemetry t{};
    Feeder(){input.telemetry=&t;input.step_khz=15000;input.step_measured=true;input.architecture=6;
        t.present=DC_T_PRIV_CLOCKS|DC_T_CORE|DC_T_XBAR|DC_T_MEM|DC_T_UTIL_GPU;t.util_gpu=99;}
    ClockReading feed(uint32_t a,uint32_t b,unsigned util=99,double gap=1.05,uint32_t id=0,uint32_t scale=1){
        response={};response.size=sizeof(response);response.count=3;
        response.rows[0]=domain(id,scale==2?"GPC2CLK":"GPC",a,b,scale);
        response.rows[1]=domain(1,"XBAR",a,b);response.rows[2]=domain(4,"MEM",a,b);
        t.core=static_cast<uint32_t>(a/1000.0/scale);t.xbar=t.mem=a/1000;t.util_gpu=util;
        input.now_seconds+=gap;++input.sample_id;input.telemetry=&t;
        return service.update(classify,input).readings[0];
    }
};
}
int main(){
    CHECK(clock_gap_band(14.4,15)=="ok"&&clock_gap_band(14.5,15)=="warn");
    CHECK(clock_gap_band(-44.5,15)=="bad"&&clock_gap_band(12.2,12.657)=="warn");
    CHECK(clock_gap_band(99,std::nullopt).empty());
    CHECK(clock_window_verdict({-14.2,-14.2,-14.2,-14.2,-14.2},15,"warn")=="warn");
    CHECK(clock_window_verdict({-13.9,-13.9,-13.9,-13.9,-13.9},15,"warn")=="ok");
    CHECK(clock_window_verdict({-14.8,-14.8,-14.8,-14.8,-265},15)=="varying");
    CHECK(std::abs(clock_median_hold({1,1,1})-0.5)<1e-9);
    Feeder jitter;ClockReading r;
    for(unsigned i=0;i<10;++i){r=jitter.feed(2115000,2087000+(i%2)*8);
        if(i<5)CHECK(r.trust=="checking");if(i==5)CHECK(r.trust=="measured");
        if(i>=5&&i<9)CHECK(r.state=="settling");}
    CHECK(r.state=="judged"&&r.verdict=="warn"&&r.window.size()==5);
    auto duplicate=jitter.service.update(classify,jitter.input).readings[0];
    CHECK(duplicate.samples==r.samples); // repeated paint/snapshot never adds evidence
    const auto previous_calls=calls;jitter.service.update(classify,jitter.input);CHECK(calls==previous_calls);
    for(size_t i=1;i<3;++i){const auto& other=jitter.service.update(classify,jitter.input).readings[i];CHECK(other.trust=="measured"&&other.state=="ungraded"&&other.verdict.empty());}
    // A private read miss retains the last value briefly but never a verdict.
    jitter.input.telemetry=nullptr;++jitter.input.sample_id;jitter.input.now_seconds+=1;
    auto data=jitter.service.update(classify,jitter.input);CHECK(data.stale&&data.rows.size()==3&&data.readings[0].stale&&data.readings[0].verdict.empty());
    jitter.input.now_seconds+=3.1;++jitter.input.sample_id;data=jitter.service.update(classify,jitter.input);
    CHECK(data.stale&&data.rows.size()==3&&!data.readings[0].available);
    r=jitter.feed(2115000,2087016);CHECK(r.trust=="measured"&&r.verdict.empty()&&r.state=="load start");
    // Even one short transient breaks the qualification window immediately;
    // the last reading/trust can remain visible stale, not judged current.
    Feeder short_gap;for(unsigned i=0;i<10;++i)short_gap.feed(2115000,2087000+(i%2)*8);
    short_gap.input.telemetry=nullptr;++short_gap.input.sample_id;short_gap.input.now_seconds+=0.5;
    data=short_gap.service.update(classify,short_gap.input);
    CHECK(data.readings[0].available&&data.readings[0].stale&&data.readings[0].window.empty());
    r=short_gap.feed(2115000,2087016,99,0.5);CHECK(r.trust=="measured"&&r.state=="load start"&&r.verdict.empty());
    // One steady equality is same; equality across an A change is mirror.
    Feeder same;for(unsigned i=0;i<13;++i)r=same.feed(1500000,1500000);CHECK(r.trust=="same"&&r.verdict.empty());
    Feeder mirror;for(uint32_t a:{648000u,648000u,4232000u,4232000u,4440000u,4440000u})r=mirror.feed(a,a,99,1.05,15,2);
    CHECK(r.trust=="mirror"&&r.scale==2&&r.a_mhz==2220&&r.verdict.empty());
    Feeder constant;for(unsigned i=0;i<13;++i)r=constant.feed(1950000,1935000);CHECK(r.trust=="unproven"&&r.verdict.empty());
    // A lagged, smoothed copy cannot earn independent evidence.
    Feeder lag;const uint32_t a[]={1950000,1965000,1980000,1980000,1980000,1995000,1995000,1995000};
    const uint32_t b[]={1950000,1950000,1950000,1965000,1980000,1980000,1980000,1995000};
    for(size_t i=0;i<8;++i)r=lag.feed(a[i],b[i]);CHECK(r.events==0&&r.trust!="measured");
    Feeder light;for(unsigned i=0;i<6;++i)r=light.feed(1950000,1949900+(i%2)*8,5);CHECK(r.trust=="measured"&&r.state=="light");
    r=light.feed(1950000,1949910,99);CHECK(r.state=="load start");
    r=light.feed(1950000,1949902,89);CHECK(r.state=="settling");
    r=light.feed(1950000,1949910,84);CHECK(r.state=="light");
    Feeder fallback;fallback.input.step_measured=false;for(unsigned i=0;i<10;++i)r=fallback.feed(2115000,2087000+(i%2)*8);
    CHECK(r.state=="judged"&&r.verdict.empty()&&!r.bin_mhz);
    Feeder stuck;for(unsigned i=0;i<10;++i)stuck.feed(2115000,2087000+(i%2)*8);
    for(unsigned i=0;i<5;++i)r=stuck.feed(2115000,2087008);CHECK(r.state=="stuck"&&r.verdict.empty());
    // Independent missing public/utility fields do not erase the table.
    jitter.feed(2115000,2087000);jitter.t.present&=~DC_T_UTIL_GPU;
    r=jitter.feed(2115000,2087008);CHECK(r.state=="load unread");
    jitter.t.present&=~DC_T_CORE;r=jitter.feed(2115000,2087000);CHECK(!r.available);
    // Malformed classifier output is a transient miss, not a lost capability.
    response.count=33;++jitter.input.sample_id;data=jitter.service.update(classify,jitter.input);CHECK(data.available&&data.stale&&!data.message.empty());
    jitter.service.reset();++jitter.input.sample_id;data=jitter.service.update(nullptr,jitter.input);CHECK(!data.available&&data.rows.empty());
    // Row unit normalization: doubled frequency versus raw PCIe geometry.
    auto doubled=domain(15,"GPC2CLK",3000000,2970000,2);auto normalized=normalize_clock_domain(doubled,15);
    CHECK(normalized.a=="3000.0 MHz"&&normalized.delta=="-30.0"&&normalized.band=="warn");
    auto pcie=domain(31,"PCIe link gen",0,123);std::snprintf(pcie.kind,sizeof(pcie.kind),"%s","pcie_gen");normalized=normalize_clock_domain(pcie,15);
    CHECK(normalized.a=="--"&&normalized.b=="raw 123"&&normalized.delta=="--"&&normalized.band.empty());
    auto unknown=domain(9,"",100000,120000,1,"unnamed");normalized=normalize_clock_domain(unknown,15);CHECK(normalized.label=="--");
    auto inferred=domain(16,"XBAR2CLK",100000,120000,2,"likely");normalized=normalize_clock_domain(inferred,15);CHECK(normalized.label=="XBAR2CLK ?");
    std::printf("clock_monitor_service_test: %u checks passed\n",checks);
}
