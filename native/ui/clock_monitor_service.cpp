// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "clock_monitor_service.h"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <deque>
#include <map>
#include <set>
namespace druta::ui {
namespace {
template<class T>void bounded(std::deque<T>& q,T value,size_t maximum){q.push_back(value);if(q.size()>maximum)q.pop_front();}
struct Evidence {
    unsigned samples=0,events=0,fresh=0,still=0,mirror_run=0;
    int direction=0;std::set<uint32_t> mirror_as;std::optional<std::pair<uint32_t,uint32_t>> prev;
    std::deque<uint32_t> recent_a;
    void break_continuity(){prev.reset();fresh=still=0;direction=0;}
};
struct Key {
    std::optional<uint32_t> domain;bool loaded=false;std::string band;double hold=0;
    std::optional<double> last_time;unsigned n=0;std::deque<unsigned> a_changes;std::vector<double> window;
    void break_continuity(){window.clear();loaded=false;band.clear();hold=0;}
};
bool valid_rows(const dc_clock_domains& rows){
    if(rows.count>DC_PRIV_DOMAINS)return false;std::set<uint32_t> domains;
    for(uint32_t i=0;i<rows.count;++i){const auto& r=rows.rows[i];
        if(r.domain>=DC_PRIV_DOMAINS||!domains.insert(r.domain).second||!r.scale||
            !std::memchr(r.name,0,sizeof(r.name))||!std::memchr(r.grade,0,sizeof(r.grade))||!std::memchr(r.kind,0,sizeof(r.kind)))return false;
        if(std::string(r.kind)!="freq"&&std::string(r.kind)!="pcie_gen")return false;
        if((r.has_prog_mhz&&!std::isfinite(r.prog_mhz))||(r.has_meas_mhz&&!std::isfinite(r.meas_mhz))||
            (r.has_delta_mhz&&!std::isfinite(r.delta_mhz)))return false;
    }return true;
}
}
struct ClockMonitorService::Impl {
    ClockMonitorData data;std::optional<uint64_t> sample;std::optional<double> last_good;
    std::array<Key,3> keys;std::map<std::pair<size_t,uint32_t>,Evidence> evidence;
    void break_samples(){
        for(size_t i=0;i<keys.size();++i){auto& k=keys[i];k.break_continuity();
            if(k.domain)evidence[{i,*k.domain}].break_continuity();}
    }
    void miss(size_t index){auto& k=keys[index];data.readings[index]={};k.break_continuity();
        if(k.domain)evidence[{index,*k.domain}].break_continuity();}
    ClockReading feed(size_t index,const dc_clock_domain_row& row,uint32_t tile,std::optional<double> util,
        double now,std::optional<double> bin){
        auto& k=keys[index];auto& ev=evidence[{index,row.domain}];
        if(k.domain!=row.domain){k.domain=row.domain;k.last_time.reset();k.break_continuity();k.a_changes.clear();ev.break_continuity();}
        if(k.last_time && (now-*k.last_time>3 || now<*k.last_time)){ev.break_continuity();k.break_continuity();}
        k.last_time=now;++k.n;++ev.samples;
        const uint32_t a=row.prog_khz,b=row.meas_khz;
        if(b==a){++ev.mirror_run;ev.mirror_as.insert(a);}else{ev.mirror_run=0;ev.mirror_as.clear();}
        if(!ev.prev){ev.fresh=ev.still=0;}
        else if(a!=ev.prev->first){ev.fresh=ev.still=0;ev.direction=0;bounded(k.a_changes,k.n,4);}
        else if(b!=ev.prev->second){++ev.fresh;ev.still=0;
            if(ev.fresh>2){const int direction=b>ev.prev->second?1:-1;
                if(ev.direction&&direction!=ev.direction&&b!=a&&std::find(ev.recent_a.begin(),ev.recent_a.end(),b)==ev.recent_a.end())++ev.events;
                ev.direction=direction;}
        }else ++ev.still;
        if(ev.recent_a.empty()||ev.recent_a.back()!=a)bounded(ev.recent_a,a,16);
        ev.prev=std::make_pair(a,b);
        ClockReading r;r.available=true;r.domain=row.domain;r.name=row.name;r.scale=row.scale;
        r.key=index==0?"core":index==1?"xbar":"mem";r.samples=ev.samples;r.events=ev.events;r.fresh=ev.fresh;r.mirror_run=ev.mirror_run;
        r.a_mhz=a/1000.0/row.scale;r.b_mhz=b/1000.0/row.scale;r.delta_mhz=(static_cast<double>(b)-a)/1000.0/row.scale;
        r.paired=std::abs(r.a_mhz-tile)<1.0;r.util=util;r.bin_mhz=bin;
        r.trust=ev.mirror_run>=5&&ev.mirror_as.size()>=2?"mirror":ev.events>=2?"measured":ev.mirror_run>=12?"same":ev.samples<12?"checking":"unproven";
        const bool loaded=util&&(*util>=90||(k.loaded&&*util>=85));
        const auto& ch=k.a_changes;const bool moving=ch.size()>=2&&ch.back()-ch[ch.size()-2]<10&&k.n-ch.back()<10;
        if(r.trust!="measured")r.state=r.trust;
        else if(index!=0)r.state="ungraded";
        else if(!util)r.state="load unread";
        else if(!loaded)r.state="light";
        else if(!k.loaded)r.state="load start";
        else if(!r.paired)r.state="unpaired";
        else if(ev.still>=5)r.state="stuck";
        else if(ev.fresh<2)r.state=moving?"moving":"settling";
        else r.state="window";
        if(r.state=="window"){
            k.window.push_back(r.delta_mhz);if(k.window.size()>15)k.window.erase(k.window.begin());
            r.state=k.window.size()>=5?"judged":moving?"moving":"settling";
        }else {k.window.clear();k.band.clear();k.hold=0;}
        k.loaded=loaded;
        if(r.state=="judged"){
            double hold=clock_median_hold(k.window);if(k.band=="warn"||k.band=="bad")hold=std::max(hold,k.hold);
            r.verdict=clock_window_verdict(k.window,bin,k.band,hold);r.hold_mhz=hold;r.window=k.window;
            if(r.verdict=="ok"||r.verdict=="warn"||r.verdict=="bad"){
                r.held=r.verdict!=clock_window_verdict(k.window,bin);k.hold=r.verdict==k.band?hold:clock_median_hold(k.window);k.band=r.verdict;
            }
        }return r;
    }
};
ClockMonitorService::ClockMonitorService():impl_(std::make_unique<Impl>()){}
ClockMonitorService::~ClockMonitorService()=default;
ClockMonitorService::ClockMonitorService(ClockMonitorService&&) noexcept=default;
ClockMonitorService& ClockMonitorService::operator=(ClockMonitorService&&) noexcept=default;
void ClockMonitorService::reset(){impl_=std::make_unique<Impl>();}
const ClockMonitorData& ClockMonitorService::update(ClockClassifier classify,const ClockMonitorInput& in){
    auto& p=*impl_;if(p.sample && *p.sample==in.sample_id)return p.data;p.sample=in.sample_id;
    const auto* t=in.telemetry;dc_clock_domains rows{};rows.size=sizeof(rows);
    std::optional<double> bin;if(in.step_measured&&in.step_khz)bin=in.step_khz/1000.0;
    const bool private_present=t&&(t->present&DC_T_PRIV_CLOCKS);
    const auto status=classify&&private_present?classify(t->priv_clocks,DC_PRIV_CLK_WORDS,(t->present&DC_T_CORE)?1:0,t->core,
        (t->present&DC_T_MEM)?1:0,t->mem,in.architecture?1:0,in.architecture.value_or(0),&rows):DC_E_UNAVAILABLE;
    if(status!=DC_OK||!valid_rows(rows)){
        p.data.stale=p.data.available;p.data.message=!classify?"Clock classifier export unavailable":!private_present?"Private clock read unavailable; last rows retained":
            "Clock classification failed or returned malformed rows; last rows retained";
        // Preserve earned counter evidence and last display briefly, but a
        // failed snapshot cannot bridge a qualified judgment window. Python
        // 1.9.1 retained short-gap continuity; this deliberately strengthens it.
        p.break_samples();
        for(auto& r:p.data.readings){r.stale=r.available;r.verdict.clear();r.window.clear();r.held=false;r.hold_mhz.reset();}
        if(!p.last_good || in.now_seconds-*p.last_good>3)for(size_t i=0;i<3;++i)p.miss(i);
        return p.data;
    }
    p.data.available=true;p.data.stale=false;p.data.message.clear();p.last_good=in.now_seconds;p.data.rows.clear();
    for(uint32_t i=0;i<rows.count;++i)p.data.rows.push_back(normalize_clock_domain(rows.rows[i],bin));
    const uint64_t bits[]={DC_T_CORE,DC_T_XBAR,DC_T_MEM};const uint32_t tile[]={t->core,t->xbar,t->mem};
    const std::optional<double> util=(t->present&DC_T_UTIL_GPU)?std::optional<double>(t->util_gpu):std::nullopt;
    for(size_t index=0;index<3;++index){const dc_clock_domain_row* selected=nullptr;
        for(uint32_t i=0;i<rows.count;++i){const auto& r=rows.rows[i];
            if(std::string(r.kind)!="freq"||std::string(r.grade)=="unpopulated"||!r.prog_khz||!r.meas_khz)continue;
            const bool match=index==0?std::string(r.grade)=="confirmed"&&(std::string(r.name)=="GPC"||std::string(r.name)=="GPC2CLK"):
                index==1?r.domain==1:std::string(r.grade)=="confirmed"&&std::string(r.name)=="MEM";
            if(match){if(selected){selected=nullptr;break;}selected=&r;}
        }
        // XBAR tile is domain 1 array A in the established telemetry ABI,
        // irrespective of whether that adapter earned an XBAR name there.
        if(selected&&(t->present&bits[index]))p.data.readings[index]=p.feed(index,*selected,tile[index],util,in.now_seconds,bin);
        else p.miss(index);
    }return p.data;
}
}
