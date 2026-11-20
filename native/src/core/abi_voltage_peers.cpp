// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/voltage_peers.h"
#include "core/abi_internal.h"
#include <algorithm>
#include <array>
#include <cstring>
#include <memory>
#include <vector>
namespace {
using namespace druta::backend;
using Fn=int(DRUTA_CDECL*)(void*,void*);
struct Guarded {std::vector<uint32_t> words;size_t length;explicit Guarded(size_t n):words((n+3)/4+32,0xa5a5a5a5),length(n){std::fill(words.begin()+16,words.end()-16,0);}uint8_t* data(){return reinterpret_cast<uint8_t*>(words.data()+16);}bool valid()const{return std::all_of(words.begin(),words.begin()+16,[](auto v){return v==0xa5a5a5a5;})&&std::all_of(words.end()-16,words.end(),[](auto v){return v==0xa5a5a5a5;});}};
void target_copy(dc_target& out,const Target& t){out={};out.size=sizeof(out);druta::abi::copy_field(out.slot,sizeof(out.slot),t.slot);out.architecture=t.architecture;out.device_id=t.device_id;out.subsystem_id=t.subsystem_id;out.handle=static_cast<uint64_t>(reinterpret_cast<uintptr_t>(t.handle));}
bool same(const Target& a,const Target& b){return a.slot==b.slot&&a.architecture==b.architecture&&a.device_id==b.device_id&&a.subsystem_id==b.subsystem_id&&a.handle==b.handle;}
bool bound(dc_gpu* h,const Target& target){const auto t=h->target.bind_client_power();return t.target&&same(*t.target,target);}
const ClkDomLayout* clock_layout(dc_gpu* h,int32_t& kind){
 const auto* layout=h->clkdom.layout();
 if(!layout){const auto issue=h->clkdom.capability_error();
  if(issue&&(issue->kind=="read_failure"||issue->kind=="unavailable")){
   // Failed discovery is cached. One explicit read-only rediscovery avoids
   // turning a temporary getter failure into permanent loss of evidence.
   h->clkdom.refresh_capabilities();layout=h->clkdom.layout();
  }
 }
 if(!layout){const auto& issue=h->clkdom.capability_error();
  kind=issue&&issue->kind=="unsupported_layout"?DC_ERR_UNSUPPORTED:
       issue&&issue->kind=="unsupported_response"?DC_ERR_MALFORMED:DC_ERR_TRANSIENT;
 }
 return layout;
}
bool packet(dc_gpu* h,Fn fn,const Target& target,Guarded& g,dc_voltage_peer_packet& state,uint32_t version,int32_t& kind,uint32_t mask_at=0,uint32_t mask=0){state.state=DC_VOLTAGE_PEER_FAILED;std::memcpy(g.data(),&version,4);if(mask_at)std::memcpy(g.data()+mask_at,&mask,4);if(!bound(h,target)){kind=DC_ERR_IDENTITY;return false;}state.status=fn(h->gpu.nvapi.gpu,g.data());if(!g.valid()){kind=DC_ERR_MALFORMED;return false;}if(!bound(h,target)){kind=DC_ERR_IDENTITY;return false;}if(state.status){kind=DC_ERR_TRANSIENT;return false;}uint32_t actual=0;std::memcpy(&actual,g.data(),4);if(actual!=version){kind=DC_ERR_MALFORMED;return false;}if(mask_at){std::memcpy(&actual,g.data()+mask_at,4);if(actual!=mask){kind=DC_ERR_MALFORMED;return false;}}state.state=DC_VOLTAGE_PEER_AVAILABLE;state.length=uint32_t(g.length);return true;}
bool capture(dc_gpu* h,const Target& target,dc_voltage_peers& out,int32_t& kind){kind=DC_ERR_TRANSIENT;target_copy(out.target,target);auto& api=h->gpu.nvapi;
 if(auto fn=api.get<Fn>(0x9df23ca1)){Guarded g(sizeof(VoltBoost));auto& st=out.voltage_boost;const uint32_t version=uint32_t(sizeof(VoltBoost))|(1u<<16);std::memcpy(g.data(),&version,4);g.data()[4]=255;st.state=DC_VOLTAGE_PEER_FAILED;if(!bound(h,target)){kind=DC_ERR_IDENTITY;return false;}st.status=fn(api.gpu,g.data());if(!g.valid()){kind=DC_ERR_MALFORMED;return false;}if(!bound(h,target)){kind=DC_ERR_IDENTITY;return false;}
  // The understood 40-byte getter has one output byte at +4. Even an
  // unsupported reply must preserve the caller-owned version/other bytes.
  if(std::memcmp(g.data(),&version,4)||!std::all_of(g.data()+5,g.data()+sizeof(VoltBoost),[](uint8_t v){return v==0;})){kind=DC_ERR_MALFORMED;return false;}
  if(st.status==-3||st.status==-9||st.status==-104){st.state=DC_VOLTAGE_PEER_UNSUPPORTED;}else if(st.status){kind=DC_ERR_TRANSIENT;return false;}else{if(g.data()[4]>100){kind=DC_ERR_MALFORMED;return false;}st.state=DC_VOLTAGE_PEER_AVAILABLE;st.length=sizeof(VoltBoost);out.voltage_boost_percent=g.data()[4];}}
 if(auto fn=api.get<Fn>(0x6ff81213)){bool done=false;for(const auto attempt:std::array<std::pair<uint32_t,uint32_t>,3>{{{7416,3},{7416,2},{7316,1}}}){Guarded g(attempt.first);const auto version=attempt.first|(attempt.second<<16);if(!packet(h,fn,target,g,out.pstates,version,kind)){if(out.pstates.status==-9&&g.valid()&&bound(h,target))continue;return false;}const auto* v=reinterpret_cast<const Pstates20V2*>(g.data());if(!v->num_pstates||v->num_pstates>16||!v->num_clocks||v->num_clocks>8||v->num_voltages>4||(attempt.first==7416&&v->num_ov_voltages>4)){out.pstates.state=DC_VOLTAGE_PEER_FAILED;kind=DC_ERR_MALFORMED;return false;}std::memcpy(out.pstates_bytes,g.data(),attempt.first);done=true;break;}if(!done){out.pstates.state=DC_VOLTAGE_PEER_FAILED;return false;}}
 if(auto fn=api.get<Fn>(0xf58938f5)){const auto* layout=clock_layout(h,kind);const auto& domains=h->clkdom.domains();if(!layout||domains.empty()){out.clock_control.state=DC_VOLTAGE_PEER_FAILED;return false;}if(layout->size>sizeof(out.clock_bytes)||layout->size<12||layout->mask_dword*4+4>layout->size){out.clock_control.state=DC_VOLTAGE_PEER_FAILED;kind=DC_ERR_MALFORMED;return false;}uint32_t mask=0;for(auto d:domains){if(d>=32){out.clock_control.state=DC_VOLTAGE_PEER_FAILED;kind=DC_ERR_MALFORMED;return false;}mask|=uint32_t(1)<<d;}Guarded g(layout->size);if(!packet(h,fn,target,g,out.clock_control,layout->version,kind,layout->mask_dword*4,mask))return false;std::memcpy(out.clock_bytes,g.data(),layout->size);out.clock_mask=mask;out.clock_version=layout->version;out.clock_header=layout->header;out.clock_stride=layout->stride;out.clock_mask_dword=layout->mask_dword;out.clock_mode=layout->mode;out.clock_freq_khz=layout->freq_khz;out.has_nvvdd_uv=layout->nvvdd_uv.has_value();out.nvvdd_uv=layout->nvvdd_uv.value_or(0);out.has_msvdd_uv=layout->msvdd_uv.has_value();out.msvdd_uv=layout->msvdd_uv.value_or(0);}
 auto boost=api.get<Fn>(0x23f1b133),curve=api.get<Fn>(0x21537ad4);if(boost||curve){const auto layout=h->curve().layout();if(!layout||layout->n_entries<2||layout->n_entries>kVfpPoints){if(boost)out.boost.state=DC_VOLTAGE_PEER_FAILED;if(curve)out.curve.state=DC_VOLTAGE_PEER_FAILED;return false;}out.vf_entries=layout->n_entries;
  for(int which=0;which<2;++which){auto fn=which?curve:boost;if(!fn)continue;auto& st=which?out.curve:out.boost;const size_t size=which?sizeof(VfpCurve):sizeof(BoostTable);Guarded g(size);uint32_t version=uint32_t(size)|(1u<<16),masks[4]{};set_point_masks(masks,layout->n_entries);std::memcpy(g.data(),&version,4);std::memcpy(g.data()+4,masks,sizeof(masks));st.state=DC_VOLTAGE_PEER_FAILED;if(!bound(h,target)){kind=DC_ERR_IDENTITY;return false;}st.status=fn(api.gpu,g.data());if(!g.valid()){kind=DC_ERR_MALFORMED;return false;}if(!bound(h,target)){kind=DC_ERR_IDENTITY;return false;}if(st.status){kind=DC_ERR_TRANSIENT;return false;}if(std::memcmp(g.data(),&version,4)||std::memcmp(g.data()+4,masks,sizeof(masks))){kind=DC_ERR_MALFORMED;return false;}if(which){const auto* v=reinterpret_cast<const VfpCurve*>(g.data());for(uint32_t i=0;i<layout->n_entries;++i)if(!v->entries[i].volt_uV||v->entries[i].freq_kHz<1000){kind=DC_ERR_MALFORMED;return false;}std::memcpy(out.curve_bytes,g.data(),size);}else std::memcpy(out.boost_bytes,g.data(),size);st.state=DC_VOLTAGE_PEER_AVAILABLE;st.length=uint32_t(size);}
 }if(!bound(h,target)){kind=DC_ERR_IDENTITY;return false;}return true;
}
}
extern "C" DC_API dc_status DC_CALL dc_voltage_peers_read(dc_gpu* h,dc_voltage_peers* out,dc_error* error){if(!h||!out||out->size!=sizeof(*out)||(error&&error->size!=sizeof(*error)))return DC_E_ARGUMENT;try{druta::LockGuard lock(h->gpu.lock());const auto target=h->target.bind_client_power();if(!target.target){if(error){error->kind=static_cast<int32_t>(target.error.kind);druta::abi::copy_field(error->message,sizeof(error->message),target.error.message);}return DC_E_FAILED;}auto first=std::make_unique<dc_voltage_peers>();auto second=std::make_unique<dc_voltage_peers>();first->size=second->size=sizeof(*out);int32_t kind=DC_ERR_TRANSIENT;const bool got_first=capture(h,*target.target,*first,kind),got_second=got_first&&capture(h,*target.target,*second,kind);std::string why;const bool ok=got_second&&druta::backend::voltage_peers_stable(*first,*second,why);*out=got_first?*second:*first;if(!ok){if(error){error->kind=got_second?DC_ERR_UNSTABLE:kind;druta::abi::copy_field(error->message,sizeof(error->message),why.empty()?"Voltage peer getter failed or returned an invalid packet":why);}return DC_E_FAILED;}if(error){error->kind=DC_ERR_NONE;error->message[0]=0;}return DC_OK;}catch(...){if(error){error->kind=DC_ERR_TRANSIENT;druta::abi::copy_field(error->message,sizeof(error->message),"Voltage peer capture failed");}return DC_E_FAILED;}}
