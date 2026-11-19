// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "monitor_model.h"
#include <algorithm>
#include <cmath>
#include <cstdio>
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
MemoryClockText memory_clock_text(const LiveData& data, std::optional<double> reported) {
    if (!reported || !std::isfinite(*reported)) return {"--", {}};
    char rate[64]{}; std::snprintf(rate, sizeof(rate), "%.2f Gbps", *reported * 2 / 1000.0);
    const std::string type = data.memory_type + " \xC2\xB7 " + rate;
    char value[64]{};
    if (data.memory_divisor && *data.memory_divisor) {
        std::snprintf(value, sizeof(value), "%.1f", *reported / *data.memory_divisor);
        std::string text = value;
        if (!text.empty() && text.back() == '0') text.pop_back();
        if (!text.empty() && text.back() == '.') text.pop_back();
        return {text, type};
    }
    std::snprintf(value, sizeof(value), "%.0f", *reported);
    return {value, type + " (raw)"};
}
MonitorModel build_monitor_model(const SessionSnapshot& s, bool classic) {
    const auto& t=s.data.telemetry; MonitorModel m;
    auto memory=scalar(s,"MEM CLOCK",DC_T_MEM,t.mem,classic?"MHz programmed":"MHz driver");
    if (classic && memory.freshness!=MonitorFreshness::unavailable) {
        const auto shown=memory_clock_text(s.data,static_cast<double>(t.mem));
        memory.text=shown.value; memory.detail=shown.detail;
    }
    // Classic follows Python TILES: "MHz programmed", "°C", whole numbers and
    // the title "POWER"; Focused keeps its driver-unit wording and decimals.
    const char* clock_unit=classic?"MHz programmed":"MHz driver";
    const char* degrees=classic?"\xC2\xB0" "C":"C";
    m.tiles={scalar(s,"CORE CLOCK",DC_T_CORE,t.core,clock_unit),
        scalar(s,"XBAR CLOCK",DC_T_XBAR,t.xbar,clock_unit),
        memory,
        scalar(s,"EDGE TEMP",DC_T_TEMP_EDGE,t.temp_edge,degrees),
        scalar(s,"HOTSPOT",DC_T_TEMP_HOTSPOT,t.temp_hotspot,degrees,classic?0:1),
        scalar(s,classic?"POWER":"BOARD POWER",DC_T_POWER_W,t.power_w,"W",classic?0:1),
        scalar(s,"VCORE",DC_T_VCORE_MV,t.vcore_mv,"mV",classic?0:2)};
    const char* keys[]={"core","xbar","mem","edge","hot","pwr","vcore"};
    for(size_t i=0;i<std::size(keys);++i)m.tiles[i].key=keys[i];
    if (classic) {
        // Python refresh_monitor subtitles: XBAR "+1515 vs core" (both clocks non-zero,
        // else "--"); HOTSPOT "Δ 11 °C over edge" once a hotspot reading exists.
        auto& xbar=m.tiles[1];
        const bool both=xbar.freshness!=MonitorFreshness::unavailable && t.xbar &&
            monitor_freshness(s,DC_T_CORE)!=MonitorFreshness::unavailable && t.core;
        xbar.detail=both?(t.xbar>=t.core?"+":"-")+std::to_string(t.xbar>=t.core?t.xbar-t.core:t.core-t.xbar)+" vs core":"--";
        auto& hot=m.tiles[4];
        if(hot.freshness!=MonitorFreshness::unavailable){
            const bool delta=monitor_freshness(s,DC_T_TEMP_DELTA)!=MonitorFreshness::unavailable && std::isfinite(t.temp_delta);
            hot.detail="\xCE\x94 "+(delta?number(t.temp_delta,0):std::string("--"))+" \xC2\xB0" "C over edge";
        }
    }
    // Same names/bits as nvbackend.EVENT_REASONS and PERF_DECREASE_BITS.
    const std::pair<uint64_t,const char*> events[]={{1,"Idle"},{2,"App clock setting"},{4,"SW power cap"},
        {8,"HW slowdown"},{16,"Sync boost"},{32,"SW thermal"},{64,"HW thermal"},{128,"HW power brake"},{256,"Display clock"}};
    const std::pair<uint64_t,const char*> perf[]={{1,"Thermal"},{2,"Power"},{4,"AC/battery"},
        {8,"API triggered"},{16,"Insufficient aux power"},{UINT64_C(0x80000000),"Unknown"}};
    m.clock_reasons=reasons(s,DC_T_EVENT_MASK,t.event_mask,events);
    m.perf_reasons=reasons(s,DC_T_PERF_DECREASE,t.perf_decrease,perf);
    auto policy=scalar(s,"Power policy target",DC_T_PL_TARGET_PCT,t.pl_target_pct,"%",1);
    auto configured=scalar(s,"Configured board request",DC_T_PL_REQUESTED,t.pl_requested_mw/1000.0,"W",3);
    auto reported=scalar(s,"Reported power limit",DC_T_PL_NOW,t.pl_now_mw/1000.0,"W",3);
    auto energy=scalar(s,"Energy counter",DC_T_ENERGY_J,t.energy_j,"J",1);
    MonitorGroup power{"Power split & utilization",{scalar(s,"GPU power",DC_T_PWR_GPU_PCT,t.pwr_gpu_pct,"%",1,true),
        scalar(s,"Board power",DC_T_PWR_BOARD_PCT,t.pwr_board_pct,"%",1,true),
        tdp_used(s),
        scalar(s,"GPU utilization",DC_T_UTIL_GPU,t.util_gpu,"%",0,true),
        scalar(s,"FB utilization",DC_T_UTIL_FB,t.util_fb,"%",0,true),
        scalar(s,"Video utilization",DC_T_UTIL_VID,t.util_vid,"%",0,true),
        scalar(s,"Bus utilization",DC_T_UTIL_BUS,t.util_bus,"%",0,true)}};
    std::vector<MonitorReading> moved;
    if (classic) {
        // Python BARS: the panel holds exactly these seven bars, short labels.
        const char* bars[]={"GPU","Board","TDP used","GPU util","FB util","VID util","BUS util"};
        for (size_t i=0;i<std::size(bars);++i) power.readings[i].label=bars[i];
        // Native-only readings stay reachable in State & sensors (Python STATE leads with energy).
        moved={energy,policy,configured,reported};
    } else {
        power.readings.insert(power.readings.begin()+3,policy);
        power.readings.insert(power.readings.end(),{configured,reported,energy});
    }
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
        scalar(s,"Temperature delta",DC_T_TEMP_DELTA,t.temp_delta,classic?"\xC2\xB0" "C":"C",1),
        scalar(s,"Voltage boost request",DC_T_VBOOST,t.vboost_pct,"%"),
        scalar(s,"Core P0 maximum",DC_T_CORE_P0MAX,t.core_p0max,"MHz driver"),
        scalar(s,"Memory P0 maximum",DC_T_MEM_P0MAX,t.mem_p0max,"MHz driver"),
        scalar(s,"V/F lock entries",DC_T_VF_LOCKED,t.vf_locked_count,""),
        scalar(s,"V/F lock voltage",DC_T_VF_LOCK_MV,t.vf_lock_mv,"mV",2),
        scalar(s,"Clock lock minimum",DC_T_CLK_LOCK,t.clk_lock_min_mhz,"MHz"),
        scalar(s,"Clock lock maximum",DC_T_CLK_LOCK,t.clk_lock_max_mhz,"MHz"),
        scalar(s,"Reported fan count",DC_T_NUM_FANS,t.num_fans,"")}};
    if (classic) {
        // Python prints memory figures as the slider and tile do: P0 maximum through
        // mem_fmt (true MHz with the adapter's divisor), the offset through
        // mem_offset_scale (2 x divisor "MHz true", else 2 "MHz eff").
        auto& p0=state.readings[7];
        if(p0.freshness!=MonitorFreshness::unavailable && s.data.memory_divisor && *s.data.memory_divisor){
            p0.text=memory_clock_text(s.data,static_cast<double>(t.mem_p0max)).value;p0.unit="MHz";
        }
        auto& offset=state.readings[3];
        if(offset.freshness!=MonitorFreshness::unavailable){
            const auto scale=classic_memory_offset_scale(s.data);char text[48]{};
            std::snprintf(text,sizeof(text),"%+g",t.mem_off/scale.first);offset.text=text;offset.unit=scale.second;
        }
    }
    state.readings.insert(state.readings.begin(),moved.begin(),moved.end());
    for(uint32_t i=0;i<std::min(t.fan_count,uint32_t{32});++i) {
        const auto& f=t.fans[i]; const auto prefix="Fan "+std::to_string(i);
        const auto duty_fresh=s.paused || (s.fan_duty_stale_mask&(UINT32_C(1)<<i))?MonitorFreshness::stale:MonitorFreshness::current;
        const auto rpm_fresh=s.paused || (s.fan_rpm_stale_mask&(UINT32_C(1)<<i))?MonitorFreshness::stale:MonitorFreshness::current;
        MonitorReading duty;duty.label=prefix+" duty";duty.unit="%";
        if(s.data.telemetry_valid && f.has_duty){duty.freshness=duty_fresh;duty.text=std::to_string(f.duty);duty.percentage=f.duty;}
        MonitorReading rpm;rpm.label=prefix+" speed";rpm.unit="RPM";
        if(s.data.telemetry_valid && f.has_rpm){rpm.freshness=rpm_fresh;rpm.text=std::to_string(f.rpm);}
        state.readings.push_back(std::move(duty));state.readings.push_back(std::move(rpm));
    }
    m.groups.push_back(std::move(state)); return m;
}
MonitorTone classic_lamp_tone(const MonitorReason& r) {
    if (!r.active || !*r.active) return MonitorTone::idle;
    return r.label == "Idle" ? MonitorTone::good : MonitorTone::bad;
}
MonitorTone classic_bar_tone(const MonitorReading& bar, MonitorTone base) {
    if (!bar.percentage || !std::isfinite(*bar.percentage)) return base;
    return *bar.percentage >= 95 ? MonitorTone::bad : *bar.percentage >= 80 ? MonitorTone::warn : base;
}
std::string classic_bar_overlay(const SessionSnapshot& s, const MonitorReading& bar, bool tdp) {
    const char* stale = bar.freshness == MonitorFreshness::stale ? " (stale)" : "";
    char text[96]{};
    if (!bar.percentage || !std::isfinite(*bar.percentage) || bar.freshness == MonitorFreshness::unavailable)
        return tdp ? "power / limit unavailable" : "--";
    if (tdp) {
        // tdp_used() only yields a percentage from a finite draw and a non-zero limit.
        const auto& t = s.data.telemetry;
        std::snprintf(text, sizeof(text), "%.0f%%   %.0f / %.0f W%s", *bar.percentage, t.power_w, t.pl_now_mw / 1000.0, stale);
    } else std::snprintf(text, sizeof(text), "%.0f%%%s", *bar.percentage, stale);
    return text;
}
MonitorTone classic_tile_tone(const SessionSnapshot& s, const MonitorReading& tile, std::optional<bool> exact) {
    if (tile.label == "XBAR CLOCK") return MonitorTone::violet;
    if (tile.label == "EDGE TEMP") return MonitorTone::good;
    if (tile.label == "HOTSPOT") {
        // Python starts the tile amber and recolours it once a reading arrives.
        const double hot = s.data.telemetry.temp_hotspot;
        if (tile.freshness == MonitorFreshness::unavailable || !std::isfinite(hot)) return MonitorTone::warn;
        return hot >= 90 ? MonitorTone::bad : hot >= 80 ? MonitorTone::warn : MonitorTone::good;
    }
    if ((tile.label == "BOARD POWER" || tile.label == "POWER") && exact) return *exact ? MonitorTone::warn : MonitorTone::bad;
    return MonitorTone::accent;
}
std::pair<double, std::string> classic_memory_offset_scale(const LiveData& data) {
    if (data.memory_divisor && *data.memory_divisor) return {2.0 * *data.memory_divisor, "MHz true"};
    return {2.0, "MHz eff"};
}
ClassicPowerTile classic_power_tile(const SessionSnapshot& s, const MonitorReading& tile, const ShuntCorrection& correction) {
    const auto& t = s.data.telemetry;
    ClassicPowerTile out;
    out.value = tile.freshness == MonitorFreshness::unavailable || tile.text.empty() ? "--" : tile.text;
    const bool has_limit = monitor_freshness(s, DC_T_PL_NOW) != MonitorFreshness::unavailable && t.pl_now_mw > 0;
    const double limit = t.pl_now_mw / 1000.0;
    char text[160]{};
    if (has_limit) std::snprintf(text, sizeof(text), "limit %.0f W", limit);
    const std::string limit_text = has_limit ? text : "limit unavailable";
    const bool draw = tile.freshness != MonitorFreshness::unavailable && std::isfinite(t.power_w);
    if (draw && correction.valid && correction.active) {
        const auto watts = correction.apply(t.power_w);
        if (watts && std::isfinite(*watts)) {
            std::snprintf(text, sizeof(text), "%.0f", *watts); out.value = text;
            std::snprintf(text, sizeof(text), "raw %.0f W x%.4g%s", t.power_w, correction.factor, correction.exact ? "" : " est");
            out.subtitle = text; out.subtitle += "\n" + limit_text;
            if (has_limit) { std::snprintf(text, sizeof(text), " = %.0f W real", limit * correction.factor); out.subtitle += text; }
            out.exact = correction.exact;
            return out;
        }
    }
    out.subtitle = limit_text;
    return out;
}
ClassicPcieSummary classic_pcie_summary(const SessionSnapshot& s) {
    const auto& t = s.data.telemetry;
    const auto model = build_monitor_model(s, true);
    const auto& rows = model.groups[1].readings;
    const auto gen = monitor_freshness(s, DC_T_PCIE_GEN), width = monitor_freshness(s, DC_T_PCIE_WIDTH);
    std::string text = "Gen " + (gen != MonitorFreshness::unavailable ? std::to_string(t.pcie_gen) : std::string("?")) +
        "  x" + (width != MonitorFreshness::unavailable ? std::to_string(t.pcie_width) : std::string("?"));
    if (gen == MonitorFreshness::stale || width == MonitorFreshness::stale) text += "  (stale)";
    // rows[2] / rows[3]: the driver-sum total and since-launch figure, unavailable
    // unless every AER aggregate was read.
    const auto& total = rows[2]; const auto& since = rows[3];
    const bool known = total.freshness != MonitorFreshness::unavailable;
    text += "\nerrors: " + (known ? total.text : std::string("--")) + "  (since launch " +
        (since.freshness != MonitorFreshness::unavailable ? since.text : std::string("--")) + ")";
    if (total.freshness == MonitorFreshness::stale) text += "  (stale)";
    if (known && t.pcie_err_total != 0) {
        // Python lists the non-zero counters by their NVML field names.
        const char* keys[] = {"correctable", "naks_rx", "receiver", "bad_tlp", "naks_tx", "bad_dllp",
            "non_fatal", "fatal", "unsupp_req", "lcrc", "lane"};
        std::string counters;
        for (size_t i = 0; i < std::size(keys); ++i) {
            const auto& r = rows[4 + i];
            if (r.freshness == MonitorFreshness::unavailable || r.text == "0") continue;
            if (!counters.empty()) counters += "  ";
            counters += std::string(keys[i]) + ":" + r.text;
        }
        if (!counters.empty()) text += "\n" + counters;
    }
    ClassicPcieSummary out;
    out.text = text;
    out.tone = !known || total.freshness == MonitorFreshness::stale ? MonitorTone::dim :
        t.pcie_err_total == 0 ? MonitorTone::good : MonitorTone::bad;
    return out;
}
std::string classic_state_summary(const SessionSnapshot& s) {
    const auto& t = s.data.telemetry;
    const auto known = [&](uint64_t bit) { return monitor_freshness(s, bit) != MonitorFreshness::unavailable; };
    // A retained (last-good) reading never reads as live: its line ends "  (stale)", as the
    // PCIe summary's do. Python has no retained readings, so its text is unchanged otherwise.
    bool stale = false;
    const auto shown = [&](uint64_t bit) { stale = stale || monitor_freshness(s, bit) == MonitorFreshness::stale; };
    const auto end_line = [&](std::string& line) { if (stale) line += "  (stale)"; stale = false; };
    char text[96]{};
    std::string out = "energy ";
    if (known(DC_T_ENERGY_J) && std::isfinite(t.energy_j)) { std::snprintf(text, sizeof(text), "%.0f", t.energy_j); out += text; shown(DC_T_ENERGY_J); }
    else out += "--";
    out += " J"; end_line(out); out += "\n";
    std::string fans;
    if (s.data.telemetry_valid) for (uint32_t i = 0; i < std::min(t.fan_count, uint32_t{32}); ++i) {
        const auto& f = t.fans[i];
        const uint32_t bit = UINT32_C(1) << i;
        if (!fans.empty()) fans += "  ";
        fans += "fan" + std::to_string(i) + ": " + (f.has_duty ? std::to_string(f.duty) : std::string("--")) + "% " +
            (f.has_rpm ? std::to_string(f.rpm) : std::string("--")) + "rpm";
        stale = stale || (f.has_duty && (s.paused || (s.fan_duty_stale_mask & bit))) ||
            (f.has_rpm && (s.paused || (s.fan_rpm_stale_mask & bit)));
    }
    out += fans.empty() ? "--" : fans;
    end_line(out);
    out += "\noffsets: core ";
    if (known(DC_T_CORE_OFF)) { std::snprintf(text, sizeof(text), "%+d", t.core_off); out += text; shown(DC_T_CORE_OFF); } else out += "--";
    const auto scale = classic_memory_offset_scale(s.data);
    out += " MHz   mem ";
    if (known(DC_T_MEM_OFF)) { std::snprintf(text, sizeof(text), "%+g", t.mem_off / scale.first); out += text; shown(DC_T_MEM_OFF); } else out += "--";
    out += " " + scale.second; end_line(out); out += "\nvolt-boost ";
    shown(DC_T_VBOOST); shown(DC_T_VF_LOCKED);  // an unread bit is never stale
    out += known(DC_T_VBOOST) ? std::to_string(t.vboost_pct) : std::string("--");
    out += "%   VF-locked ";
    const bool locks_read = known(DC_T_VF_LOCKED);
    if (!locks_read) out += "--";
    else if (!t.vf_locked_count) out += "none";
    else {
        out += "[";
        for (uint32_t i = 0; i < std::min(t.vf_locked_count, uint32_t{32}); ++i)
            out += (i ? ", " : "") + std::to_string(t.vf_locked_domains[i]);
        out += "]";
    }
    if (known(DC_T_VF_LOCK_MV) && std::isfinite(t.vf_lock_mv) && t.vf_lock_mv != 0) {
        std::snprintf(text, sizeof(text), " (asked %.2f mV)", t.vf_lock_mv); out += text; shown(DC_T_VF_LOCK_MV);
    }
    // No clock lock in a lock table that was read is "none"; with the table unread
    // too, the state is unknown.
    if (known(DC_T_CLK_LOCK)) {
        std::snprintf(text, sizeof(text), "   clk-locked [%u..%u] MHz", t.clk_lock_min_mhz, t.clk_lock_max_mhz); out += text;
        shown(DC_T_CLK_LOCK);
    } else out += locks_read ? "   clk-locked none" : "   clk-locked --";
    end_line(out);
    return out;
}
ClassicMonitorLayout classic_monitor_layout(float page, float tiles, float dom_content, float px, float chrome) {
    ClassicMonitorLayout out;
    if (!std::isfinite(page)) page = 0;
    if (!std::isfinite(tiles)) tiles = 0;
    if (!std::isfinite(dom_content)) dom_content = 0;
    chrome = std::isfinite(chrome) && chrome >= 0 ? std::ceil(chrome) : std::floor(102 * px);
    out.bot = std::max(std::floor(110 * px), std::floor(page * 0.12f));
    const float share = std::max(std::floor(page * 0.34f), std::ceil(dom_content));
    out.dom = std::max(std::floor(240 * px), std::min(share, std::floor(page - tiles - out.bot - chrome - std::floor(220 * px))));
    out.mid = std::max(std::floor(220 * px), std::floor(page - tiles - out.dom - out.bot - chrome));
    return out;
}
}
