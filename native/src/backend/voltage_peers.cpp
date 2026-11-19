// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/voltage_peers.h"
#include <algorithm>
#include <cstring>
namespace druta::backend {
bool voltage_peers_stable(const dc_voltage_peers& a,const dc_voltage_peers& b,std::string& error){
 const auto target=[](const dc_target& x,const dc_target& y){return x.size==sizeof(x)&&y.size==sizeof(y)&&std::memcmp(x.slot,y.slot,sizeof(x.slot))==0&&x.architecture==y.architecture&&x.device_id==y.device_id&&x.subsystem_id==y.subsystem_id&&x.handle==y.handle;};
 if(!target(a.target,b.target)){error="Voltage peer target changed";return false;}
 const auto equal=[](const dc_voltage_peer_packet& x,const dc_voltage_peer_packet& y,const uint8_t* p,const uint8_t* q,size_t cap){return x.state<=DC_VOLTAGE_PEER_AVAILABLE&&x.state==y.state&&x.length==y.length&&x.length<=cap&&((x.state==DC_VOLTAGE_PEER_ABSENT&&x.length==0)||(x.state==DC_VOLTAGE_PEER_AVAILABLE&&x.length>0&&std::equal(p,p+x.length,q)));};
 if(a.voltage_boost.state!=b.voltage_boost.state||a.voltage_boost.state==DC_VOLTAGE_PEER_FAILED||a.voltage_boost.state>DC_VOLTAGE_PEER_UNSUPPORTED||a.voltage_boost.status!=b.voltage_boost.status||a.voltage_boost.length!=b.voltage_boost.length||a.voltage_boost_percent!=b.voltage_boost_percent){error="Voltage boost request unavailable or changed between reads";return false;}
 if(!equal(a.pstates,b.pstates,a.pstates_bytes,b.pstates_bytes,sizeof(a.pstates_bytes))||!equal(a.clock_control,b.clock_control,a.clock_bytes,b.clock_bytes,sizeof(a.clock_bytes))||!equal(a.boost,b.boost,a.boost_bytes,b.boost_bytes,sizeof(a.boost_bytes))||a.curve.state==DC_VOLTAGE_PEER_FAILED||b.curve.state==DC_VOLTAGE_PEER_FAILED||a.curve.state!=b.curve.state||a.vf_entries!=b.vf_entries||a.clock_mask!=b.clock_mask||a.clock_version!=b.clock_version||a.clock_header!=b.clock_header||a.clock_stride!=b.clock_stride||a.clock_mask_dword!=b.clock_mask_dword||a.clock_mode!=b.clock_mode||a.clock_freq_khz!=b.clock_freq_khz||a.has_nvvdd_uv!=b.has_nvvdd_uv||a.nvvdd_uv!=b.nvvdd_uv||a.has_msvdd_uv!=b.has_msvdd_uv||a.msvdd_uv!=b.msvdd_uv){error="Voltage peer requests unavailable or changed between reads";return false;}
 return true;
}
}
