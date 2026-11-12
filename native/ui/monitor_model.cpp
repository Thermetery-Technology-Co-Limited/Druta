// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "monitor_model.h"
#include <algorithm>
#include <cmath>
#include <iomanip>
#include <sstream>

namespace druta::ui {
MonitorFreshness monitor_freshness(const SessionSnapshot& s, uint64_t bit) {
    if (!s.data.telemetry_valid || !(s.data.telemetry.present & bit)) return MonitorFreshness::unavailable;
    return s.paused || (s.telemetry_stale_mask & bit) ? MonitorFreshness::stale : MonitorFreshness::current;
}
namespace {
std::string number(double value, int decimals) {
    std::ostringstream text; text << std::fixed << std::setprecision(decimals) << value; return text.str();
}
std::string hex(uint64_t value) {
    std::ostringstream text; text << "0x" << std::uppercase << std::hex << value; return text.str();
}
MonitorReading scalar(const SessionSnapshot& s, const char* label, uint64_t bit, double value,
    const char* unit, int decimals = 0, bool percent = false) {
    MonitorReading r; r.label = label; r.unit = unit; r.freshness = monitor_freshness(s,bit);
    if (!std::isfinite(value)) r.freshness = MonitorFreshness::unavailable;
    if (r.freshness != MonitorFreshness::unavailable) { r.text = number(value,decimals); if (percent) r.percentage = value; }
    return r;
}
MonitorReading text_reading(const SessionSnapshot& s, const char* label, uint64_t bit, std::string text) {
    MonitorReading r; r.label=label; r.freshness=monitor_freshness(s,bit);
    if (r.freshness != MonitorFreshness::unavailable) r.text=std::move(text);
    return r;
}
MonitorReading tdp_used(const SessionSnapshot& s) {
    MonitorReading r;r.label="TDP used";r.unit="%";
    const auto draw=monitor_freshness(s,DC_T_POWER_W),limit=monitor_freshness(s,DC_T_PL_NOW);
    const auto& t=s.data.telemetry;
    // PowerPolStatus target_pct is a configured policy target, not load.
    // Match Python's actual board draw / reported enforced limit calculation.
    if(draw==MonitorFreshness::unavailable || limit==MonitorFreshness::unavailable ||
        !std::isfinite(t.power_w) || !t.pl_now_mw)return r;
    const double value=t.power_w/(t.pl_now_mw/1000.0)*100.0;
    if(!std::isfinite(value))return r;
    r.freshness=draw==MonitorFreshness::stale || limit==MonitorFreshness::stale?MonitorFreshness::stale:MonitorFreshness::current;
    r.text=number(value,1);r.percentage=value;return r;
}
template<size_t N> std::vector<MonitorReason> reasons(const SessionSnapshot& s, uint64_t present,
    uint64_t mask, const std::pair<uint64_t,const char*> (&names)[N]) {
    std::vector<MonitorReason> result; uint64_t known=0; const auto freshness=monitor_freshness(s,present);
    for (const auto& entry:names) {
        known|=entry.first; MonitorReason r; r.label=entry.second; r.freshness=freshness;
        if(freshness!=MonitorFreshness::unavailable)r.active=(mask&entry.first)!=0;
        result.push_back(std::move(r));
    }
    if(freshness!=MonitorFreshness::unavailable && (mask&~known))
        result.push_back({"Unrecognized bits " + hex(mask&~known),true,freshness});
    return result;
}
MonitorReading typed_counter(const SessionSnapshot& s, const char* label, const dc_typed_value& value) {
    auto r=text_reading(s,label,DC_T_PCIE_ERR,{});
    if(!value.present)r.freshness=MonitorFreshness::unavailable;
    if(r.freshness!=MonitorFreshness::unavailable) {
        if(value.type==0 && std::isfinite(value.d)){
            std::ostringstream text;text<<std::setprecision(17)<<value.d;r.text=text.str();
        }
        else if(value.type>=1 && value.type<=3)r.text=std::to_string(value.u);
        else if(value.type==4 || value.type==5)r.text=std::to_string(value.s);
        else r.freshness=MonitorFreshness::unavailable;
    }
    return r;
}
}
MonitorModel build_monitor_model(const SessionSnapshot& s) {
    const auto& t=s.data.telemetry; MonitorModel m;
    m.tiles={scalar(s,"CORE CLOCK",DC_T_CORE,t.core,"MHz driver"),
        scalar(s,"XBAR CLOCK",DC_T_XBAR,t.xbar,"MHz driver"),
        scalar(s,"MEM CLOCK",DC_T_MEM,t.mem,"MHz driver"),
        scalar(s,"EDGE TEMP",DC_T_TEMP_EDGE,t.temp_edge,"C"),
        scalar(s,"HOTSPOT",DC_T_TEMP_HOTSPOT,t.temp_hotspot,"C",1),
        scalar(s,"BOARD POWER",DC_T_POWER_W,t.power_w,"W",1),
        scalar(s,"VCORE",DC_T_VCORE_MV,t.vcore_mv,"mV",2)};
    // Same names/bits as nvbackend.EVENT_REASONS and PERF_DECREASE_BITS.
    const std::pair<uint64_t,const char*> events[]={{1,"Idle"},{2,"App clock setting"},{4,"SW power cap"},
        {8,"HW slowdown"},{16,"Sync boost"},{32,"SW thermal"},{64,"HW thermal"},{128,"HW power brake"},{256,"Display clock"}};
    const std::pair<uint64_t,const char*> perf[]={{1,"Thermal"},{2,"Power"},{4,"AC/battery"},
        {8,"API triggered"},{16,"Insufficient aux power"},{UINT64_C(0x80000000),"Unknown"}};
    m.clock_reasons=reasons(s,DC_T_EVENT_MASK,t.event_mask,events);
    m.perf_reasons=reasons(s,DC_T_PERF_DECREASE,t.perf_decrease,perf);
    MonitorGroup power{"Power split & utilization",{scalar(s,"GPU power",DC_T_PWR_GPU_PCT,t.pwr_gpu_pct,"%",1,true),
        scalar(s,"Board power",DC_T_PWR_BOARD_PCT,t.pwr_board_pct,"%",1,true),
        tdp_used(s),
        scalar(s,"Power policy target",DC_T_PL_TARGET_PCT,t.pl_target_pct,"%",1),
        scalar(s,"GPU utilization",DC_T_UTIL_GPU,t.util_gpu,"%",0,true),
        scalar(s,"FB utilization",DC_T_UTIL_FB,t.util_fb,"%",0,true),
        scalar(s,"Video utilization",DC_T_UTIL_VID,t.util_vid,"%",0,true),
        scalar(s,"Bus utilization",DC_T_UTIL_BUS,t.util_bus,"%",0,true),
        scalar(s,"Configured board request",DC_T_PL_REQUESTED,t.pl_requested_mw/1000.0,"W",3),
        scalar(s,"Reported power limit",DC_T_PL_NOW,t.pl_now_mw/1000.0,"W",3),
        scalar(s,"Energy counter",DC_T_ENERGY_J,t.energy_j,"J",1)}};
    m.groups.push_back(std::move(power));
    MonitorGroup pcie{"PCIe link & errors",{scalar(s,"Link generation",DC_T_PCIE_GEN,t.pcie_gen,""),
        scalar(s,"Link width",DC_T_PCIE_WIDTH,t.pcie_width,"lanes"),
        scalar(s,"Error total (driver sum)",DC_T_PCIE_ERR,t.pcie_err_total,"",0),
        scalar(s,"Errors since baseline (coverage unverified)",DC_T_PCIE_ERR,t.pcie_err_since,"",0)}};
    // read_pcie sums only the available correctable/non_fatal/fatal AER
    // contributors. The group-present bit alone cannot establish a complete
    // total (or even that any contributor was read). Do not display its zero as
    // a verified all-clear. Historical baseline coverage is not in the ABI.
    bool complete_aer=true;
    for(size_t i:{size_t{0},size_t{6},size_t{7}})
        complete_aer=complete_aer && typed_counter(s,"",t.pcie_err[i]).freshness!=MonitorFreshness::unavailable;
    if(!complete_aer)for(size_t i:{size_t{2},size_t{3}}){
        pcie.readings[i].text.clear();pcie.readings[i].freshness=MonitorFreshness::unavailable;
    }
    const char* counter_names[]={"Correctable","NAKs received","Receiver","Bad TLP","NAKs sent","Bad DLLP",
        "Non-fatal","Fatal","Unsupported request","LCRC","Lane"};
    for(size_t i=0;i<std::size(counter_names);++i)pcie.readings.push_back(typed_counter(s,counter_names[i],t.pcie_err[i]));
    m.groups.push_back(std::move(pcie));
    MonitorGroup state{"State & sensors",{text_reading(s,"Performance state",DC_T_PSTATE,"P"+std::to_string(t.pstate)),
        scalar(s,"Video clock",DC_T_VIDEO,t.video,"MHz driver"),
        scalar(s,"Core offset",DC_T_CORE_OFF,t.core_off,"MHz driver"),
        scalar(s,"Memory offset",DC_T_MEM_OFF,t.mem_off,"MHz driver"),
        scalar(s,"Temperature delta",DC_T_TEMP_DELTA,t.temp_delta,"C",1),
        scalar(s,"Voltage boost request",DC_T_VBOOST,t.vboost_pct,"%"),
        scalar(s,"Core P0 maximum",DC_T_CORE_P0MAX,t.core_p0max,"MHz driver"),
        scalar(s,"Memory P0 maximum",DC_T_MEM_P0MAX,t.mem_p0max,"MHz driver"),
        scalar(s,"V/F lock entries",DC_T_VF_LOCKED,t.vf_locked_count,""),
        scalar(s,"V/F lock voltage",DC_T_VF_LOCK_MV,t.vf_lock_mv,"mV",2),
        scalar(s,"Clock lock minimum",DC_T_CLK_LOCK,t.clk_lock_min_mhz,"MHz"),
        scalar(s,"Clock lock maximum",DC_T_CLK_LOCK,t.clk_lock_max_mhz,"MHz"),
        scalar(s,"Reported fan count",DC_T_NUM_FANS,t.num_fans,"")}};
    for(uint32_t i=0;i<std::min(t.fan_count,uint32_t{32});++i) {
        const auto& f=t.fans[i]; const auto prefix="Fan "+std::to_string(i);
        const auto fresh=s.paused || s.fan_readings_stale?MonitorFreshness::stale:MonitorFreshness::current;
        MonitorReading duty;duty.label=prefix+" duty";duty.unit="%";
        if(s.data.telemetry_valid && f.has_duty){duty.freshness=fresh;duty.text=std::to_string(f.duty);duty.percentage=f.duty;}
        MonitorReading rpm;rpm.label=prefix+" speed";rpm.unit="RPM";
        if(s.data.telemetry_valid && f.has_rpm){rpm.freshness=fresh;rpm.text=std::to_string(f.rpm);}
        state.readings.push_back(std::move(duty));state.readings.push_back(std::move(rpm));
    }
    m.groups.push_back(std::move(state)); return m;
}
}
