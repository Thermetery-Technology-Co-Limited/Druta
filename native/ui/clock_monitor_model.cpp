// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "clock_monitor_model.h"
#include <algorithm>
#include <cmath>
#include <iomanip>
#include <sstream>
namespace druta::ui {
namespace {
double median(std::vector<double> values){
    std::sort(values.begin(),values.end());const auto n=values.size();
    return n%2?values[n/2]:(values[n/2-1]+values[n/2])/2;
}
std::string number(double value,int decimals=1,bool sign=false){
    std::ostringstream text;if(sign)text<<std::showpos;
    text<<std::fixed<<std::setprecision(decimals)<<(value==0?0.0:value);return text.str();
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
std::string clock_secondary_text(const ClockReading& r,bool stale){
    if(!r.available)return "B: --";
    const bool last=stale||r.stale;
    std::string text;
    if(last)text="last B "+number(r.b_mhz)+" MHz";
    else if(r.trust=="checking")text="B: checking";
    else if(r.trust=="mirror")text="B: none (B = A)";
    else if(r.trust=="same")text="B: same as A";
    else text=(r.trust=="measured"?"measured ":"unproven ")+number(r.b_mhz)+" MHz";
    if(last||r.trust=="measured"||r.trust=="unproven")text+=" / delta "+number(r.delta_mhz,1,true);
    text+=" / "+(last?"stale":r.state);
    if(!last && !r.verdict.empty())text+=" / "+r.verdict;
    return text;
}
std::string clock_secondary_explanation(const ClockReading& r){
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
    return text.str();
}
}
