// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Pure snapshot behavior: no DLL, GPU, window or polling.
#include "monitor_model.h"
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <limits>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
static const MonitorReading& row(const MonitorModel& m,const char* label){
    for(const auto& tile:m.tiles)if(tile.label==label)return tile;
    for(const auto& group:m.groups)for(const auto& r:group.readings)if(r.label==label)return r;
    std::abort();
}
int main(){
    SessionSnapshot s;auto m=build_monitor_model(s);
    CHECK(row(m,"CORE CLOCK").freshness==MonitorFreshness::unavailable);
    CHECK(!m.clock_reasons[0].active.has_value());
    s.data.telemetry_valid=true;auto& t=s.data.telemetry;
    t.present=DC_T_CORE|DC_T_XBAR|DC_T_TEMP_HOTSPOT|DC_T_PWR_BOARD_PCT|DC_T_UTIL_FB|
        DC_T_PL_REQUESTED|DC_T_EVENT_MASK|DC_T_PERF_DECREASE|DC_T_PCIE_ERR|DC_T_PSTATE;
    t.core=0;t.xbar=1234;t.temp_hotspot=84.5;t.pwr_board_pct=127.5;t.util_fb=0;t.pl_requested_mw=123456;
    t.event_mask=UINT64_C(0x4000000000000000)|4;t.perf_decrease=0x80000000;
    t.pcie_err[0].present=1;t.pcie_err[0].type=3;t.pcie_err[0].u=UINT64_MAX;
    t.pcie_err[1].present=1;t.pcie_err[1].type=5;t.pcie_err[1].s=INT64_MIN;
    t.pcie_err[2].present=1;t.pcie_err[2].type=0;t.pcie_err[2].d=1.25;
    t.pcie_err[3].present=1;t.pcie_err[3].type=99;
    s.telemetry_stale_mask=DC_T_XBAR|DC_T_PCIE_ERR;
    m=build_monitor_model(s);
    CHECK(row(m,"CORE CLOCK").text=="0" && row(m,"CORE CLOCK").freshness==MonitorFreshness::current);
    CHECK(row(m,"XBAR CLOCK").freshness==MonitorFreshness::stale && row(m,"XBAR CLOCK").text=="1234");
    CHECK(row(m,"HOTSPOT").text=="84.5" && row(m,"HOTSPOT").unit=="C");
    CHECK(row(m,"MEM CLOCK").freshness==MonitorFreshness::unavailable);
    CHECK(row(m,"Configured board request").text=="123.456" && row(m,"Configured board request").unit=="W");
    CHECK(row(m,"FB utilization").percentage==0.0);
    CHECK(row(m,"Board power").percentage==127.5); // never clamp the displayed request to a bar
    CHECK(row(m,"Correctable").text=="18446744073709551615" && row(m,"Correctable").freshness==MonitorFreshness::stale);
    CHECK(row(m,"NAKs received").text=="-9223372036854775808");
    CHECK(row(m,"Receiver").text=="1.25");
    CHECK(row(m,"Bad TLP").freshness==MonitorFreshness::unavailable);
    CHECK(row(m,"Fatal").freshness==MonitorFreshness::unavailable); // absent counter within present error group
    CHECK(row(m,"Error total (driver sum)").freshness==MonitorFreshness::unavailable);
    CHECK(m.clock_reasons.size()==10 && m.clock_reasons.back().label=="Unrecognized bits 0x4000000000000000");
    CHECK(m.clock_reasons[2].active==true && m.clock_reasons[0].active==false);
    CHECK(m.perf_reasons.back().label=="Unknown" && m.perf_reasons.back().active==true);
    CHECK(row(m,"Performance state").text=="P0");
    t.present&=~DC_T_EVENT_MASK;m=build_monitor_model(s);
    CHECK(m.clock_reasons.size()==9 && !m.clock_reasons[2].active.has_value());
    t.temp_hotspot=std::numeric_limits<double>::quiet_NaN();m=build_monitor_model(s);
    CHECK(row(m,"HOTSPOT").freshness==MonitorFreshness::unavailable);
    // Session can retain established values on transient failure: stale bits
    // qualify each retained value independently; no capability coupling.
    s.paused=true;m=build_monitor_model(s);
    CHECK(row(m,"CORE CLOCK").freshness==MonitorFreshness::stale);
    CHECK(row(m,"MEM CLOCK").freshness==MonitorFreshness::unavailable);
    s.paused=false;s.fan_readings_stale=true;s.fan_rpm_stale_mask=1;t.fan_count=1;t.fans[0].has_rpm=1;t.fans[0].rpm=0;
    m=build_monitor_model(s);CHECK(row(m,"Fan 0 speed").text=="0" && row(m,"Fan 0 speed").freshness==MonitorFreshness::stale);
    CHECK(row(m,"Fan 0 duty").freshness==MonitorFreshness::unavailable);
    t.fans[0].has_duty=1;t.fans[0].duty=42;m=build_monitor_model(s);
    CHECK(row(m,"Fan 0 duty").freshness==MonitorFreshness::current && row(m,"Fan 0 speed").freshness==MonitorFreshness::stale);
    s.fan_rpm_stale_mask=0;s.fan_duty_stale_mask=1;m=build_monitor_model(s);
    CHECK(row(m,"Fan 0 duty").freshness==MonitorFreshness::stale && row(m,"Fan 0 speed").freshness==MonitorFreshness::current);
    t.fan_count=UINT32_MAX;m=build_monitor_model(s);CHECK(m.groups.back().readings.size()==13+64);
    // Different adapter/identity and only PCIe capabilities must remain useful.
    s={};s.data.telemetry_valid=true;s.selected_slot="0000:02:00.0";s.data.telemetry.present=DC_T_PCIE_GEN|DC_T_PCIE_WIDTH;
    s.data.telemetry.pcie_gen=4;s.data.telemetry.pcie_width=8;m=build_monitor_model(s);
    CHECK(row(m,"Link generation").text=="4" && row(m,"Link width").text=="8");
    CHECK(row(m,"CORE CLOCK").freshness==MonitorFreshness::unavailable);
    // A group-present packet with no AER contributors must not invent zero.
    t.present=DC_T_PCIE_ERR;s.telemetry_stale_mask=0;m=build_monitor_model(s);
    CHECK(row(m,"Error total (driver sum)").freshness==MonitorFreshness::unavailable);
    for(size_t i:{size_t{0},size_t{6},size_t{7}}){t.pcie_err[i].present=1;t.pcie_err[i].type=3;t.pcie_err[i].u=0;}
    m=build_monitor_model(s);CHECK(row(m,"Error total (driver sum)").text=="0");
    CHECK(row(m,"Errors since baseline (coverage unverified)").text=="0");
    t.pcie_err[6].type=99;m=build_monitor_model(s);
    CHECK(row(m,"Error total (driver sum)").freshness==MonitorFreshness::unavailable);
    CHECK(row(m,"Correctable").text=="0");
    // TDP use is actual board draw / enforced limit, never policy target.
    s={};s.data.telemetry_valid=true;
    t.present=DC_T_POWER_W|DC_T_PL_NOW|DC_T_PL_TARGET_PCT;t.power_w=10;t.pl_now_mw=200000;t.pl_target_pct=618.5;
    m=build_monitor_model(s);CHECK(row(m,"TDP used").text=="5.0" && row(m,"TDP used").percentage==5.0);
    CHECK(row(m,"Power policy target").text=="618.5" && !row(m,"Power policy target").percentage.has_value());
    s.telemetry_stale_mask=DC_T_POWER_W;m=build_monitor_model(s);
    CHECK(row(m,"TDP used").freshness==MonitorFreshness::stale && row(m,"Power policy target").freshness==MonitorFreshness::current);
    s.telemetry_stale_mask=DC_T_PL_NOW;m=build_monitor_model(s);CHECK(row(m,"TDP used").freshness==MonitorFreshness::stale);
    s.telemetry_stale_mask=DC_T_PL_TARGET_PCT;m=build_monitor_model(s);CHECK(row(m,"TDP used").freshness==MonitorFreshness::current);
    t.pl_now_mw=0;m=build_monitor_model(s);CHECK(row(m,"TDP used").freshness==MonitorFreshness::unavailable);
    t.pl_now_mw=200000;t.present&=~DC_T_PL_NOW;m=build_monitor_model(s);
    CHECK(row(m,"TDP used").freshness==MonitorFreshness::unavailable && !row(m,"TDP used").percentage.has_value());
    t.present=DC_T_PL_NOW;m=build_monitor_model(s);CHECK(row(m,"TDP used").freshness==MonitorFreshness::unavailable);
    t.present=DC_T_POWER_W|DC_T_PL_NOW;t.power_w=0;m=build_monitor_model(s);CHECK(row(m,"TDP used").text=="0.0");
    t.power_w=std::numeric_limits<double>::infinity();m=build_monitor_model(s);CHECK(row(m,"TDP used").freshness==MonitorFreshness::unavailable);
    std::printf("monitor_model_test: %u checks passed\n",checks);
}
