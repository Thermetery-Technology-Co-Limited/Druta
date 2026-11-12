// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Real pure C ABI integration: no GPU handle, driver calls or writes.
#include "clock_monitor_service.h"
#include <cstdio>
#include <cstdlib>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
int main(){
    ClockMonitorService service;dc_telemetry t{};t.present=DC_T_PRIV_CLOCKS|DC_T_CORE|DC_T_MEM;
    t.core=1500;t.mem=7000;
    auto set=[&](unsigned domain,uint32_t a,uint32_t b){t.priv_clocks[2*domain]=a;t.priv_clocks[64+7*domain]=b;};
    set(0,1500000,1499000);set(4,7000000,6999000);set(9,333000,332000);set(31,4,17);
    ClockMonitorInput in;in.telemetry=&t;in.sample_id=1;in.architecture=6;
    auto data=service.update(dc_clock_domains_classify,in);CHECK(data.available);
    bool core=false,unknown=false,pcie=false;for(const auto& r:data.rows){
        if(r.raw.domain==0)core=r.label=="GPC"&&r.raw.scale==1;
        if(r.raw.domain==9)unknown=r.label=="--"&&r.raw.prog_khz==333000;
        if(r.raw.domain==31)pcie=r.a=="gen 4"&&r.b=="raw 17"&&r.delta=="--";
    }CHECK(core&&unknown&&pcie);
    // A different architectural layout keeps its doubled units and empty
    // modern slot instead of borrowing the first adapter's GPC binding.
    service.reset();t.priv_clocks[0]=t.priv_clocks[64]=0;set(15,3000000,2990000);
    in.architecture=3;++in.sample_id;data=service.update(dc_clock_domains_classify,in);
    CHECK(data.readings[0].domain==15&&data.readings[0].scale==2&&data.readings[0].a_mhz==1500);
    // Unknown architecture uses the classifier's own unique-correlation rules.
    service.reset();in.architecture.reset();++in.sample_id;data=service.update(dc_clock_domains_classify,in);
    CHECK(data.available&&data.readings[0].domain==15);
    std::printf("clock_monitor_classifier_test: %u checks passed\n",checks);
}
