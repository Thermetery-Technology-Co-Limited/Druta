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
static bool has(const std::string& text,const char* part){return text.find(part)!=std::string::npos;}
// Classic / Verbose follows Python 1.9.1 refresh_monitor wording and number formats.
static void classic_formats(){
    SessionSnapshot s;s.data.telemetry_valid=true;auto& t=s.data.telemetry;
    t.present=DC_T_CORE|DC_T_XBAR|DC_T_TEMP_EDGE|DC_T_TEMP_HOTSPOT|DC_T_TEMP_DELTA|DC_T_POWER_W|DC_T_PL_NOW|DC_T_VCORE_MV;
    t.core=225;t.xbar=1740;t.temp_edge=27;t.temp_hotspot=38.4;t.temp_delta=10.6;t.power_w=76.6;t.pl_now_mw=900000;t.vcore_mv=900.004;
    auto m=build_monitor_model(s,true);
    CHECK(row(m,"CORE CLOCK").text=="225" && row(m,"CORE CLOCK").unit=="MHz programmed" && row(m,"XBAR CLOCK").unit=="MHz programmed");
    CHECK(row(m,"EDGE TEMP").unit=="\xC2\xB0" "C" && row(m,"HOTSPOT").text=="38" && row(m,"HOTSPOT").unit=="\xC2\xB0" "C");
    CHECK(row(m,"POWER").text=="77" && row(m,"POWER").key=="pwr" && row(m,"VCORE").text=="900");
    CHECK(row(m,"XBAR CLOCK").detail=="+1515 vs core" && row(m,"HOTSPOT").detail=="\xCE\x94 11 \xC2\xB0" "C over edge");
    // Focused keeps its driver wording and decimals under the old title.
    auto focused=build_monitor_model(s);
    CHECK(row(focused,"BOARD POWER").text=="76.6" && row(focused,"CORE CLOCK").unit=="MHz driver" && row(focused,"VCORE").text=="900.00");
    // POWER tile: limit line unmodded; raw, factor and real ceiling with a shunt correction.
    ShuntCorrection none;auto power=classic_power_tile(s,row(m,"POWER"),none);
    CHECK(power.value=="77" && power.subtitle=="limit 900 W" && !power.exact);
    ShuntCorrection mod;mod.valid=mod.active=mod.exact=true;mod.factor=3.499;
    power=classic_power_tile(s,row(m,"POWER"),mod);
    CHECK(power.value=="268" && power.subtitle=="raw 77 W x3.499\nlimit 900 W = 3149 W real" && power.exact==true);
    mod.exact=false;CHECK(classic_power_tile(s,row(m,"POWER"),mod).subtitle=="raw 77 W x3.499 est\nlimit 900 W = 3149 W real");
    // A missing channel keeps Python's placeholders; an unread limit is said.
    t.present&=~(DC_T_CORE|DC_T_TEMP_DELTA|DC_T_PL_NOW|DC_T_POWER_W);m=build_monitor_model(s,true);
    CHECK(row(m,"XBAR CLOCK").detail=="--" && row(m,"HOTSPOT").detail=="\xCE\x94 -- \xC2\xB0" "C over edge");
    power=classic_power_tile(s,row(m,"POWER"),mod);CHECK(power.value=="--" && power.subtitle=="limit unavailable" && !power.exact);
    t.present&=~DC_T_TEMP_HOTSPOT;m=build_monitor_model(s,true);CHECK(row(m,"HOTSPOT").detail.empty());
    // PCIe summary: two good lines when the AER total is complete and zero.
    s={};s.data.telemetry_valid=true;
    t.present=DC_T_PCIE_GEN|DC_T_PCIE_WIDTH|DC_T_PCIE_ERR;t.pcie_gen=4;t.pcie_width=16;
    auto pcie=classic_pcie_summary(s);
    CHECK(pcie.text=="Gen 4  x16\nerrors: --  (since launch --)" && pcie.tone==MonitorTone::dim); // no contributors read
    for(size_t i:{size_t{0},size_t{6},size_t{7}}){t.pcie_err[i].present=1;t.pcie_err[i].type=3;t.pcie_err[i].u=0;}
    pcie=classic_pcie_summary(s);CHECK(pcie.text=="Gen 4  x16\nerrors: 0  (since launch 0)" && pcie.tone==MonitorTone::good);
    t.pcie_err[0].u=3;t.pcie_err[3].present=1;t.pcie_err[3].type=3;t.pcie_err[3].u=2;t.pcie_err_total=3;t.pcie_err_since=1;
    pcie=classic_pcie_summary(s);
    CHECK(pcie.text=="Gen 4  x16\nerrors: 3  (since launch 1)\ncorrectable:3  bad_tlp:2" && pcie.tone==MonitorTone::bad);
    t.present=0;pcie=classic_pcie_summary(s);CHECK(has(pcie.text,"Gen ?  x?") && pcie.tone==MonitorTone::dim);
    // State summary: memory offset in the slider's units through the adapter divisor.
    s={};s.data.telemetry_valid=true;s.data.memory_divisor=8;
    t.present=DC_T_ENERGY_J|DC_T_CORE_OFF|DC_T_MEM_OFF|DC_T_VBOOST|DC_T_VF_LOCKED|DC_T_MEM_P0MAX;
    t.energy_j=747345.4;t.core_off=0;t.mem_off=1000;t.vboost_pct=0;t.vf_locked_count=0;t.mem_p0max=10501;
    t.fan_count=2;t.fans[0]={1,30,1,0};t.fans[1]={1,42,0,0};
    CHECK(classic_state_summary(s)=="energy 747345 J\nfan0: 30% 0rpm  fan1: 42% --rpm\n"
        "offsets: core +0 MHz   mem +62.5 MHz true\nvolt-boost 0%   VF-locked none   clk-locked none");
    // A retained (last-good) reading never reads as live: its line says so, the rest stay as Python's.
    s.telemetry_stale_mask=DC_T_ENERGY_J;s.fan_rpm_stale_mask=1;
    CHECK(classic_state_summary(s)=="energy 747345 J  (stale)\nfan0: 30% 0rpm  fan1: 42% --rpm  (stale)\n"
        "offsets: core +0 MHz   mem +62.5 MHz true\nvolt-boost 0%   VF-locked none   clk-locked none");
    s.telemetry_stale_mask=DC_T_VF_LOCKED;s.fan_rpm_stale_mask=2;  // fan1 has no RPM to be stale
    CHECK(classic_state_summary(s)=="energy 747345 J\nfan0: 30% 0rpm  fan1: 42% --rpm\n"
        "offsets: core +0 MHz   mem +62.5 MHz true\nvolt-boost 0%   VF-locked none   clk-locked none  (stale)");
    s.telemetry_stale_mask=0;s.fan_rpm_stale_mask=0;s.paused=true;
    CHECK(has(classic_state_summary(s),"J  (stale)\nfan0") && has(classic_state_summary(s),"true  (stale)\nvolt"));
    s.paused=false;
    m=build_monitor_model(s,true);
    CHECK(row(m,"Memory offset").text=="+62.5" && row(m,"Memory offset").unit=="MHz true");
    CHECK(row(m,"Memory P0 maximum").text=="1312.6" && row(m,"Memory P0 maximum").unit=="MHz");
    CHECK(build_monitor_model(s).groups[2].readings[3].text=="1000"); // Focused keeps driver units
    t.present|=DC_T_VF_LOCK_MV|DC_T_CLK_LOCK;t.vf_locked_count=2;t.vf_locked_domains[0]=0;t.vf_locked_domains[1]=4;
    t.vf_lock_mv=950;t.clk_lock_min_mhz=1200;t.clk_lock_max_mhz=1800;
    CHECK(has(classic_state_summary(s),"VF-locked [0, 4] (asked 950.00 mV)   clk-locked [1200..1800] MHz"));
    s.data.memory_divisor.reset();t.present=DC_T_MEM_OFF;t.fan_count=0;
    CHECK(classic_state_summary(s)=="energy -- J\n--\noffsets: core -- MHz   mem +500 MHz eff\nvolt-boost --%   VF-locked --   clk-locked --");
    s.data.telemetry_valid=false;CHECK(has(classic_state_summary(s),"mem -- MHz eff"));
    // relayout(): bands share the page; the domain panel grows to its rows while the
    // middle row keeps its 220 px floor, and every band has its floor on a short page.
    auto layout=classic_monitor_layout(1180,140,0,1);
    CHECK(layout.bot==141 && layout.dom==401 && layout.mid==396);
    layout=classic_monitor_layout(1060,140,420,1);
    CHECK(layout.bot==127 && layout.dom==420 && layout.mid==271);
    layout=classic_monitor_layout(1060,140,900,1);CHECK(layout.dom==1060-140-127-102-220 && layout.mid==220);
    layout=classic_monitor_layout(300,140,0,1);CHECK(layout.bot==110 && layout.dom==240 && layout.mid==220);
    // Measured native chrome replaces Python's 102 px: the middle row gets the difference.
    layout=classic_monitor_layout(1060,140,420,1,60);
    CHECK(layout.bot==127 && layout.dom==420 && layout.mid==271+42);
    layout=classic_monitor_layout(1060,140,420,1,59.5f);CHECK(layout.mid==313); // never overfills
    for(const float unmeasured:{-1.0f,NAN,INFINITY})CHECK(classic_monitor_layout(1060,140,420,1,unmeasured).mid==271);
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
    // Python mem_fmt: true MHz only with the adapter's divisor; otherwise raw and marked.
    t.present=DC_T_MEM;t.mem=14001;s.telemetry_stale_mask=0;s.paused=false;
    m=build_monitor_model(s);CHECK(row(m,"MEM CLOCK").text=="14001" && row(m,"MEM CLOCK").unit=="MHz driver" && row(m,"MEM CLOCK").detail.empty());
    m=build_monitor_model(s,true);
    CHECK(row(m,"MEM CLOCK").text=="14001" && row(m,"MEM CLOCK").unit=="MHz programmed" && row(m,"MEM CLOCK").detail=="unknown \xC2\xB7 28.00 Gbps (raw)");
    s.data.memory_divisor=2;s.data.memory_type="HBM2";m=build_monitor_model(s,true);
    CHECK(row(m,"MEM CLOCK").text=="7000.5" && row(m,"MEM CLOCK").detail=="HBM2 \xC2\xB7 28.00 Gbps");
    t.mem=14000;m=build_monitor_model(s,true);CHECK(row(m,"MEM CLOCK").text=="7000");
    CHECK(memory_clock_text(s.data,std::nullopt).value=="--" && memory_clock_text(s.data,std::nullopt).detail.empty());
    t.present=0;m=build_monitor_model(s,true);CHECK(row(m,"MEM CLOCK").freshness==MonitorFreshness::unavailable && row(m,"MEM CLOCK").text.empty());
    // Classic: Python's seven BARS only; the other power readings move to State & sensors.
    s={};s.data.telemetry_valid=true;
    t.present=DC_T_POWER_W|DC_T_PL_NOW|DC_T_PL_TARGET_PCT|DC_T_ENERGY_J|DC_T_PWR_BOARD_PCT|DC_T_UTIL_GPU|DC_T_TEMP_HOTSPOT;
    t.power_w=77.4;t.pl_now_mw=900000;t.pl_target_pct=100;t.energy_j=5;t.pwr_board_pct=127.5;t.util_gpu=80;t.temp_hotspot=79.9;
    m=build_monitor_model(s,true);
    const char* bars[]={"GPU","Board","TDP used","GPU util","FB util","VID util","BUS util"};
    CHECK(m.groups[0].readings.size()==7);
    for(size_t i=0;i<7;++i)CHECK(m.groups[0].readings[i].label==bars[i]);
    CHECK(m.groups[2].readings[0].label=="Energy counter" && m.groups[2].readings[1].label=="Power policy target"
        && m.groups[2].readings[2].label=="Configured board request" && m.groups[2].readings[3].label=="Reported power limit");
    CHECK(build_monitor_model(s).groups[0].readings.size()==11); // Focused grouping unchanged
    const auto& power=m.groups[0].readings;
    CHECK(classic_bar_overlay(s,power[2],true)=="9%   77 / 900 W");
    CHECK(classic_bar_overlay(s,power[1],false)=="128%" && classic_bar_tone(power[1],MonitorTone::violet)==MonitorTone::bad);
    CHECK(classic_bar_tone(power[3],MonitorTone::good)==MonitorTone::warn);
    CHECK(classic_bar_overlay(s,power[0],false)=="--" && classic_bar_tone(power[0],MonitorTone::accent)==MonitorTone::accent);
    s.telemetry_stale_mask=DC_T_UTIL_GPU;m=build_monitor_model(s,true);
    CHECK(classic_bar_overlay(s,m.groups[0].readings[3],false)=="80% (stale)");
    t.present&=~DC_T_PL_NOW;m=build_monitor_model(s,true);
    CHECK(classic_bar_overlay(s,m.groups[0].readings[2],true)=="power / limit unavailable");
    // Lamps: asserted Idle green, other asserted red, clear or unread dark.
    MonitorReason lamp{"Idle",true,MonitorFreshness::current};CHECK(classic_lamp_tone(lamp)==MonitorTone::good);
    lamp.label="SW power cap";CHECK(classic_lamp_tone(lamp)==MonitorTone::bad);
    lamp.active=false;CHECK(classic_lamp_tone(lamp)==MonitorTone::idle);
    lamp.active.reset();CHECK(classic_lamp_tone(lamp)==MonitorTone::idle);
    // Tile colours: hotspot 80 / 90 C thresholds, POWER by shunt correction kind.
    s.telemetry_stale_mask=0;m=build_monitor_model(s,true);
    CHECK(classic_tile_tone(s,row(m,"HOTSPOT"))==MonitorTone::good);
    t.temp_hotspot=80;m=build_monitor_model(s,true);CHECK(classic_tile_tone(s,row(m,"HOTSPOT"))==MonitorTone::warn);
    t.temp_hotspot=90;m=build_monitor_model(s,true);CHECK(classic_tile_tone(s,row(m,"HOTSPOT"))==MonitorTone::bad);
    t.present&=~DC_T_TEMP_HOTSPOT;m=build_monitor_model(s,true);CHECK(classic_tile_tone(s,row(m,"HOTSPOT"))==MonitorTone::warn);
    CHECK(classic_tile_tone(s,row(m,"XBAR CLOCK"))==MonitorTone::violet && classic_tile_tone(s,row(m,"EDGE TEMP"))==MonitorTone::good);
    CHECK(classic_tile_tone(s,row(m,"POWER"))==MonitorTone::accent && classic_tile_tone(s,row(m,"POWER"),true)==MonitorTone::warn
        && classic_tile_tone(s,row(m,"POWER"),false)==MonitorTone::bad && classic_tile_tone(s,row(m,"VCORE"))==MonitorTone::accent);
    classic_formats();
    std::printf("monitor_model_test: %u checks passed\n",checks);
}
