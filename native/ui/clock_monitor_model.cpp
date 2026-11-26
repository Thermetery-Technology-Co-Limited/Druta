// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "clock_monitor_model.h"
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <iomanip>
#include <sstream>
namespace druta::ui {
namespace {
double median(std::vector<double> values){
    std::sort(values.begin(),values.end());const auto n=values.size();
    return n%2?values[n/2]:(values[n/2-1]+values[n/2])/2;
}
std::string number(double value,int decimals=1,bool sign=false){
    // Python round(v, n) + 0.0: the fixed-point print rounds half-to-even on
    // the binary value as round() does; a result that rounds to zero is then
    // printed as positive zero, so a value a hair below zero never shows "-0.0".
    auto print=[&](double v){std::ostringstream text;if(sign)text<<std::showpos;
        text<<std::fixed<<std::setprecision(decimals)<<v;return text.str();};
    std::string text=print(value);
    if(std::isfinite(value)&&text.find_first_not_of("+-0.")==std::string::npos)text=print(0.0);
    return text;
}
std::string hex(uint32_t value){std::ostringstream text;text<<"0x"<<std::uppercase<<std::hex<<value;return text.str();}
bool usable(std::optional<double> bin){return bin && std::isfinite(*bin) && *bin>0;}
}
std::string clock_gap_band(double gap,std::optional<double> bin){
    if(!usable(bin)||!std::isfinite(gap))return {};
    const double g=std::abs(gap);return g>=3 * *bin-0.5?"bad":g>=*bin-0.5?"warn":"ok";
}
double clock_median_hold(const std::vector<double>& deltas){
    if(deltas.size()<2)return 0.5;
    std::vector<double> steps;for(size_t i=1;i<deltas.size();++i)steps.push_back(std::abs(deltas[i]-deltas[i-1]));
    return 0.5+3*1.2533*(median(steps)/0.954)/std::sqrt(static_cast<double>(deltas.size()));
}
std::string clock_window_verdict(const std::vector<double>& deltas,std::optional<double> bin,
    const std::string& previous,std::optional<double> hold){
    if(deltas.empty()||!usable(bin))return {};
    const auto start=deltas.end()-static_cast<std::ptrdiff_t>(std::min(deltas.size(),size_t{5}));
    const auto bounds=std::minmax_element(start,deltas.end());if(*bounds.second-*bounds.first>*bin)return "varying";
    const double g=std::abs(median(deltas));auto band=clock_gap_band(g,bin);const double release=hold.value_or(clock_median_hold(deltas));
    if(previous=="bad"&&band=="warn"&&g>=3 * *bin-0.5-release)band="bad";
    else if((previous=="warn"||previous=="bad")&&band=="ok"&&g>=*bin-0.5-release)band="warn";
    return band;
}
ClockDomainViewRow normalize_clock_domain(const dc_clock_domain_row& r,std::optional<double> bin){
    ClockDomainViewRow v;v.raw=r;v.label=r.name[0]?r.name:"--";
    if(std::string(r.grade)=="likely")v.label+=" ?";
    v.flags=hex(r.flags);v.source=hex(r.srcid);
    if(std::string(r.kind)=="pcie_gen"){
        v.a=r.prog_khz?"gen "+std::to_string(r.prog_khz):"--";v.b="raw "+std::to_string(r.meas_khz);v.delta="--";
    }else if(std::string(r.grade)=="unpopulated")v.a=v.b=v.delta="--";
    else {
        v.a=r.has_prog_mhz?number(r.prog_mhz)+" MHz":"--";
        v.b=r.has_meas_mhz?number(r.meas_mhz)+" MHz":"--";
        v.delta=r.has_delta_mhz?number(r.delta_mhz,1,true):"--";
        if(r.has_delta_mhz)v.band=clock_gap_band(r.delta_mhz/std::max(r.scale,1u),bin);
    }
    return v;
}
std::string clock_secondary_text(const ClockReading& r,bool stale,double divisor){
    if(!r.available)return "B: --";
    const bool last=stale||r.stale;
    const double div=std::isfinite(divisor)&&divisor>0?divisor:1.0;
    std::string text;
    if(last)text="last B "+number(r.b_mhz/div)+" MHz";
    else if(r.trust=="checking")text="B: checking";
    else if(r.trust=="mirror")text="B: none (B = A)";
    else if(r.trust=="same")text="B: same as A";
    else text=(r.trust=="measured"?"measured ":"unproven ")+number(r.b_mhz/div)+" MHz";
    if(last||r.trust=="measured"||r.trust=="unproven")text+=" / delta "+number(r.delta_mhz/div,1,true);
    text+=" / "+(last?"stale":r.state);
    if(!last && !r.verdict.empty())text+=" / "+r.verdict;
    return text;
}
std::string clock_secondary_explanation(const ClockReading& r,double divisor){
    if(!r.available)return "No usable paired private row on this adapter in this reading.";
    std::ostringstream text;text<<"Private domain "<<r.domain<<" "<<r.name<<". A/B divided by row scale "<<r.scale
        <<" into this tile's driver MHz. A "<<r.a_mhz<<", B "<<r.b_mhz<<" MHz. "
        <<"B earned "<<r.events<<" independent direction reversals in "<<r.samples<<" snapshots; trust: "<<r.trust<<". "
        <<"Two reversals after two initial refreshes, away from recent A values, earn measured on this adapter. "
        <<"Identical B through A changes withdraws that claim. Load gate 90% (exit 85%), five settling readings, "
        <<"and an independently measured device clock bin are required for a core verdict. "
        <<"These are evidence policies, not universal physical guarantees. XBAR/memory deltas are never graded.";
    if(r.bin_mhz)text<<" Clock bin "<<*r.bin_mhz<<" MHz.";
    else text<<" Device clock bin unavailable; no gap severity is claimed.";
    if(r.held)text<<" Verdict held by median-noise hysteresis.";
    if(std::isfinite(divisor)&&divisor>0&&divisor!=1)text<<" Shown divided by "<<divisor<<", as the tile is.";
    return text.str();
}
ClassicClockLine classic_clock_line(const ClockReading* r,bool stale,bool failed,double divisor){
    ClassicClockLine out;
    if(!r||!r->available){out.line="measured: --";return out;}
    const double div=std::isfinite(divisor)&&divisor>0?divisor:1.0;
    // Python real_delta_text: one decimal (never "-0.0") below 100 MHz, whole above.
    const double dv=r->delta_mhz/div;
    std::string delta=number(dv,1,true);
    if(std::abs(std::round(dv*10)/10)>=100){char text[32]{};std::snprintf(text,sizeof(text),"%+.0f",std::nearbyint(dv));delta=text;}
    const std::string figure=number(r->b_mhz/div,0)+"  \xCE\x94 "+delta;
    if(r->trust=="checking")out.line="measured: checking B";
    else if(r->trust=="mirror")out.line="measured: none (B = A)";
    else if(r->trust=="same")out.line="measured: same as A";
    else if(r->trust=="unproven")out.line="unproven "+figure;
    else out.line="measured "+figure;
    if(failed&&(r->trust=="measured"||r->trust=="unproven"))out.line="last "+figure;
    static const std::pair<const char*,const char*> tokens[]={{"checking"," \xC2\xB7 checking"},
        {"same"," \xC2\xB7 same as A"},{"mirror"," \xC2\xB7 B = A"},{"unproven"," \xC2\xB7 unproven"},
        {"settling"," \xC2\xB7 settling"},{"load start"," \xC2\xB7 settling"},{"moving"," \xC2\xB7 clock moving"},
        {"stuck"," \xC2\xB7 not refreshing"},{"load unread"," \xC2\xB7 load unread"}};
    const auto token=[&](const std::string& key){for(const auto& t:tokens)if(key==t.first)return std::string(t.second);return std::string();};
    if(stale)out.token=" \xC2\xB7 stale";
    else if(failed)out.token=" \xC2\xB7 read failed";
    else if(r->trust!="measured")out.token=token(r->trust);
    else if(r->state=="judged")out.token=r->verdict=="varying"?" \xC2\xB7 varying":!r->verdict.empty()?" \xC2\xB7 steady at load":"";
    else if(r->state=="light"){
        if(r->util&&std::isfinite(*r->util)){char text[48]{};std::snprintf(text,sizeof(text)," \xC2\xB7 load %.0f %%",*r->util);out.token=text;}
        else out.token=token("load unread");
    }else if(r->state=="unpaired"){char text[48]{};std::snprintf(text,sizeof(text)," \xC2\xB7 vs A %.0f",r->a_mhz/div);out.token=text;}
    else out.token=token(r->state);
    return out;
}
std::vector<std::string> classic_clock_line_templates(bool raw_memory){
    std::vector<std::string> out={"measured 0000  \xCE\x94 -00.0","measured 0000  \xCE\x94 +00.0",
        "measured 0000  \xCE\x94 -0000","measured 0000  \xCE\x94 +0000","unproven 0000  \xCE\x94 +00.0",
        "last 0000  \xCE\x94 +0000","measured: none (B = A)","measured: not read yet","measured: checking B",
        "measured: same as A","measured: n/a"};
    // a memory clock with no known divisor is shown raw, and can have five digits
    if(raw_memory)out.push_back("measured 00000  \xCE\x94 +00000");
    return out;
}
const std::vector<std::string>& classic_core_tokens(){
    static const std::vector<std::string> tokens={" \xC2\xB7 steady at load"," \xC2\xB7 varying"," \xC2\xB7 settling",
        " \xC2\xB7 load 100 %"," \xC2\xB7 load unread"," \xC2\xB7 stale"," \xC2\xB7 read failed"," \xC2\xB7 checking",
        " \xC2\xB7 same as A"," \xC2\xB7 B = A"," \xC2\xB7 unproven"," \xC2\xB7 clock moving"," \xC2\xB7 not refreshing",
        " \xC2\xB7 vs A 0000"};
    return tokens;
}
namespace {
// Python realclock's policy constants, as ClockMonitorService applies them.
constexpr int load_gate_pct=90,load_exit_pct=85,window_reads=5,median_reads=15,fresh_needed=2,evidence_events=2,stuck_reads=5;
constexpr const char* gap_tol="0.5";constexpr double gap_tol_mhz=0.5;
constexpr const char* read_gap_s="3";constexpr const char* pair_tol_mhz="1";
// The XBAR tile's number is array A of this private slot; ClockMonitorService pairs the same.
constexpr uint32_t xbar_tile_domain=1;
std::string g(double value){char text[32]{};std::snprintf(text,sizeof(text),"%g",value);return text;}
}
std::string classic_clock_tooltip(const ClassicClockTip& in){
    const ClockReading* r=in.reading&&in.reading->available?in.reading:nullptr;
    if(!r){
        // Python real_clock_view: no rows in this snapshot, no figure on the tile, or no
        // row on this card that the tile can pair with.
        if(!in.rows)return in.error.empty()?"No private clock reading yet.":
            in.error+". The private clock getter is the only source these tiles use for a measured clock.";
        if(!in.tile)return "This tile has no figure on this card, so there is nothing to pair a measurement with.";
        if(in.key=="core")return "No row on this card is CONFIRMED as the core clock (GPC or GPC2CLK) with both arrays "
            "non-zero in this reading. Names are earned per card; see ALL CLOCK DOMAINS.";
        if(in.key=="xbar")return "Private domain "+std::to_string(xbar_tile_domain)+" is empty or absent in this reading.";
        if(in.key=="mem")return "No row on this card is CONFIRMED as MEM with both arrays non-zero.";
        return {};
    }
    const double div=in.key=="mem"&&std::isfinite(in.divisor)&&in.divisor>0?in.divisor:1.0;
    const std::string& trust=r->trust;const std::string& state=r->state;const std::string& verdict=r->verdict;
    const std::optional<double> bin=usable(r->bin_mhz)?r->bin_mhz:std::nullopt;
    const bool two=r->scale==2,last=in.failed&&(trust=="measured"||trust=="unproven");
    const double a_raw=r->a_mhz*r->scale,b_raw=r->b_mhz*r->scale;
    const std::string shown=in.tile?*in.tile+" MHz":"none this time";
    std::vector<std::string> lines;
    if(in.key=="core")lines.push_back("The number above is the driver's current clock, "+shown+" (public NVAPI, or private "
        "array A where that is missing). On one TITAN RTX, NVML and GPU-Z showed this same programmed figure.");
    else if(in.key=="xbar")lines.push_back("The number above is array A of private domain "+std::to_string(r->domain)+", "+shown+".");
    else lines.push_back("The number above is the driver's current memory clock ("+in.tile.value_or("--")+
        (in.memory_divided?" MHz":", raw driver units")+").");
    lines.push_back("Row: private domain "+std::to_string(r->domain)+" "+(r->name.empty()?"--":r->name)+
        (two?" (a 2CLK row, halved to core MHz here)":""));
    lines.push_back("A (programmed)  "+number(a_raw)+" MHz"+(two?" = 2 x "+number(a_raw/2):""));
    lines.push_back("B (second array)  "+number(b_raw)+" MHz"+(two?" = 2 x "+number(b_raw/2):""));
    const std::string delta="\xCE\x94 B-A "+number(r->delta_mhz,1,true)+" MHz";
    if(in.key=="mem")lines.push_back("Memory clock differences are shown in MHz. Memory bin spacing is unknown, and "
        "on one TITAN RTX the two memory arrays differed by a fixed -6.8 MHz (raw) in every state, loaded or idle - so a "
        "memory \xCE\x94 is not by itself a lower memory clock."+(div!=1.0?" Shown divided by "+g(div)+", as the tile is.":""));
    else if(in.key=="xbar")lines.push_back(delta+". Shown, never judged: XBAR follows the core clock, and its own clock bin "
        "is not known here.");
    else if(bin)lines.push_back(delta+" = "+number(r->delta_mhz / *bin,2,true)+" bins of "+number(*bin,3)+
        " MHz (this card's clock step)");
    else lines.push_back(delta+". Clock Monitor needs an independent measurement of this card's clock step to grade this difference.");
    const std::string ev=std::to_string(r->events),n=std::to_string(r->samples),run=std::to_string(r->mirror_run),
        need=std::to_string(evidence_events);
    const std::string counted="B turning back on its own - up after down, or down after up - while A holds still, to a "
        "value A never reported, after its first "+std::to_string(fresh_needed)+" moves since A last changed (a copy of A "
        "a few reads late, offset or smoothed, only moves one way by then)";
    if(trust=="measured")lines.push_back("B counts as measured on this card: while A held still it turned back on its own "+
        ev+" times, to values A never reported ("+n+" readings).");
    else if(trust=="checking")lines.push_back("Checking whether B is a counter on this card. What counts is "+counted+
        " ("+ev+" of "+need+" so far, "+n+" readings).");
    else if(trust=="same")lines.push_back("B has equalled A exactly in the last "+run+" readings while A held still, so it "
        "cannot yet be told apart from a copy of A. If A changes and B follows it exactly, that is what this says.");
    else if(trust=="mirror"&&r->events>=static_cast<unsigned>(evidence_events))lines.push_back("B equals A exactly right "
        "now, through a change of A ("+run+" readings), although it turned back on its own "+ev+" times earlier; no "
        "measured number is shown while it copies A.");
    else if(trust=="mirror")lines.push_back("B has equalled A exactly in the last "+run+" readings, through a change of A, "
        "so on this card it has not shown itself to be an independent measurement and no measured clock is shown "
        "(GK104/GM107 did this in every tested state).");
    else lines.push_back(std::string(r->b_mhz==r->a_mhz?"B equals A right now, and has not yet shown what counts - ":
        "B differs from A, but has not yet shown what counts - ")+counted+" ("+ev+" of "+need+" in "+n+" readings)"+
        " - so it is shown as unproven, not as a measurement.");
    const auto& w=r->window;
    if(in.failed)lines.push_back(std::string("The private clock read failed this time; the last good ")+
        (last?"values are shown as \"last\"":"state is kept")+", not judged, for up to "+read_gap_s+" s.");
    else if(in.stale)lines.push_back("Not judged: the latest driver read failed; these are the last good values.");
    else if(trust=="measured"&&in.key=="core"){
        const std::string gate=std::to_string(load_gate_pct),below=std::to_string(load_exit_pct);
        if(state=="judged"&&verdict=="varying"&&!w.empty()){
            const auto span=std::minmax_element(w.begin(),w.end());
            lines.push_back("Not coloured: within the last "+std::to_string(w.size())+" readings \xCE\x94 ran "+
                number(*span.first,1,true)+" to "+number(*span.second,1,true)+" MHz, more than one bin apart.");
        }else if(state=="judged"&&!verdict.empty()&&!w.empty()){
            const auto span=std::minmax_element(w.begin(),w.end());
            const double util=r->util.value_or(0);
            const auto load=r->window_util.value_or(std::make_pair(util,util));
            lines.push_back("Judged over the last "+std::to_string(w.size())+" readings at "+number(load.first,0)+"-"+
                number(load.second,0)+" % load with A unchanged: \xCE\x94 "+number(*span.first,1,true)+" to "+
                number(*span.second,1,true)+" MHz, median "+number(median(w),1,true)+".");
            if(r->held&&bin){
                const double edge=(verdict=="bad"?3:1) * *bin-gap_tol_mhz;
                const double hold=r->hold_mhz&&*r->hold_mhz?*r->hold_mhz:clock_median_hold(w);
                lines.push_back(std::string(verdict=="bad"?"Red":"Amber")+" is HELD: the median crossed the "+number(edge)+
                    " MHz edge earlier and has not fallen "+number(hold)+" MHz back past it (the tolerance plus three "
                    "standard errors of the median, the widest seen while held), so the colour does not flicker on the edge.");
            }
        }else if(state=="settling"||state=="load start")lines.push_back("Not judged yet: needs "+
            std::to_string(window_reads)+" readings at >= "+gate+" % load (a chosen threshold; once reached, the load "
            "counts until it drops below "+below+" %) with A unchanged, after B has refreshed "+std::to_string(fresh_needed)+
            " times since A last changed (on one TU102, B trailed a clock change by 1-2 s).");
        else if(state=="moving")lines.push_back("Not judged: the programmed clock keeps changing (GPU Boost moving between "
            "bins), so there is no steady target to compare B with.");
        else if(state=="stuck")lines.push_back("Not judged: B has not changed in "+std::to_string(stuck_reads)+" or more "
            "readings while A held still, so it may not be refreshing.");
        else if(state=="light")lines.push_back("Not judged below "+gate+" % load ("+number(r->util.value_or(0),0)+" % now; "
            "a load once reached counts until it drops below "+below+" %) - chosen thresholds. At idle the clock gates "
            "between bursts and B reads low (one TU102 read 470-573 MHz at a 1350 MHz lock).");
        else if(state=="load unread")lines.push_back("Not judged: GPU load is unavailable in this reading.");
        else if(state=="unpaired")lines.push_back("Not judged: this row's A ("+number(r->a_mhz)+") differs from the number "
            "above ("+in.tile.value_or("None")+") by "+pair_tol_mhz+" MHz or more, so \xCE\x94 is against A.");
    }
    if(in.key=="core")lines.push_back("Colour, from the median of up to "+std::to_string(median_reads)+" readings at load "
        "under one A: plain = within one clock bin, amber = one bin or more, red = three or more, either sign (above the "
        "programmed clock counts too), "+std::string(gap_tol)+" MHz short of an edge counting as reaching it; a colour "
        "once reached is held until the median falls clearly back past its edge, and says so; dim = not judged. B behaved "
        "as a physical counter on one TITAN RTX (TU102); elsewhere 'measured' means B passed the check above, not a "
        "calibrated instrument.");
    std::string text;
    for(const auto& line:lines){if(!text.empty())text+='\n';text+=line;}
    return text;
}
ClassicDomainCells classic_domain_cells(const ClockDomainViewRow& v){
    ClassicDomainCells c;const auto& r=v.raw;char text[32]{};
    std::snprintf(text,sizeof(text),"%2u",r.domain);c.index=text;
    const std::string grade=r.grade;
    c.name=grade=="likely"?std::string(r.name)+"?":r.name[0]?std::string(r.name):"--";
    c.grade_tone=grade=="confirmed"?"text":grade=="likely"?"warn":"dim";
    c.a=v.a;c.b=v.b;
    c.delta=v.delta=="--"||v.delta.empty()?"--":v.delta+" MHz";
    std::snprintf(text,sizeof(text),r.flags<=0xFF?"0x%02X":"0x%08X",r.flags);c.flags=text;
    c.srcid=std::to_string(r.srcid);
    return c;
}
}
