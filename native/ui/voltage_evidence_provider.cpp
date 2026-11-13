// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_evidence_provider.h"
#include "vbios_voltage_graph.h"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <memory>
namespace druta::ui {
namespace {
using Bytes=std::vector<uint8_t>;using Availability=VoltagePolicyAvailability;
[[noreturn]]void fail(const std::string& message,Availability kind=Availability::invalid){throw VoltageEquationEvidenceError(message,kind);}
void require(bool value,const char* message){if(!value)fail(message);}
Availability kind(const dc_error& error){return error.kind==DC_ERR_TRANSIENT||error.kind==DC_ERR_UNSTABLE?Availability::transient:error.kind==DC_ERR_UNSUPPORTED?Availability::unsupported:Availability::invalid;}
bool terminated(const char* text,size_t capacity){return std::memchr(text,0,capacity)!=nullptr;}
uint32_t u32(const uint8_t* bytes){return uint32_t(bytes[0])|uint32_t(bytes[1])<<8|uint32_t(bytes[2])<<16|uint32_t(bytes[3])<<24;}
uint16_t u16(const uint8_t* bytes){return uint16_t(bytes[0]|uint16_t(bytes[1])<<8);}
float f32(const uint8_t* bytes){auto raw=u32(bytes);float value;std::memcpy(&value,&raw,4);return value;}
void append(Bytes& out,uint32_t value){for(unsigned i=0;i<4;++i)out.push_back(uint8_t(value>>(8*i)));}
bool same_target(const dc_target& a,const dc_target& b){return a.size==sizeof(a)&&b.size==sizeof(b)&&terminated(a.slot,sizeof(a.slot))&&terminated(b.slot,sizeof(b.slot))&&a.slot[0]&&a.handle&&a.handle==b.handle&&std::strcmp(a.slot,b.slot)==0&&a.architecture==b.architecture&&a.device_id==b.device_id&&a.subsystem_id==b.subsystem_id;}
int64_t microvolts(double mv,bool signed_value=false){const auto value=mv*1000.0;const auto rounded=std::round(value);
 if(!std::isfinite(value)||std::abs(value-rounded)>1e-6||rounded<(signed_value?double(INT32_MIN):0.0)||rounded>(signed_value?double(INT32_MAX):double(UINT32_MAX)))fail("Rail policy has noncanonical microvolt units");return static_cast<int64_t>(rounded);}
bool present(const Bytes& info,uint32_t index){return index<8192&&(info[4+index/8]&(1u<<(index%8)));}
struct RailRound {std::array<VoltageEquationRail,2> rails;Bytes identity,requests;};
struct Provider {
 VoltageEvidenceApi api;dc_gpu_identity opened;
 Provider(VoltageEvidenceApi value,const dc_gpu_identity& identity):api(std::move(value)),opened(identity){}
 dc_target binding(dc_gpu* gpu,const dc_target& expected){
  if(!api.target.identity||!api.target.bind||!api.target.architecture_supported)fail("Paired voltage evidence interfaces unavailable",Availability::unsupported);
  dc_gpu_identity id{};id.size=sizeof(id);const auto status=api.target.identity(gpu,&id);if(status!=DC_OK)fail("Voltage evidence identity read failed",status==DC_E_ARGUMENT?Availability::invalid:Availability::transient);
  require(opened.size==sizeof(opened)&&id.size==sizeof(id)&&terminated(opened.slot,sizeof(opened.slot))&&terminated(opened.uuid,sizeof(opened.uuid))&&terminated(id.slot,sizeof(id.slot))&&terminated(id.uuid,sizeof(id.uuid))&&terminated(id.pairing_error,sizeof(id.pairing_error))&&terminated(opened.pairing_error,sizeof(opened.pairing_error)),"Voltage evidence identity strings are malformed");
  require(id.slot[0]&&id.nvapi_ok&&id.nvapi_selected&&!id.pairing_error[0]&&!opened.pairing_error[0]&&std::strcmp(opened.slot,id.slot)==0&&std::strcmp(opened.uuid,id.uuid)==0&&opened.nvapi_handle==id.nvapi_handle&&opened.nvapi_devid==id.nvapi_devid&&opened.nvapi_subsys==id.nvapi_subsys&&opened.nvml_selected==id.nvml_selected&&opened.nvml_has_devid==id.nvml_has_devid&&opened.nvml_devid==id.nvml_devid&&opened.nvml_subsys==id.nvml_subsys,"Voltage evidence target identity or paired handle changed");
  dc_target target{};target.size=sizeof(target);dc_error error{};error.size=sizeof(error);if(api.target.bind(gpu,1,&target,&error)!=DC_OK)fail("Voltage evidence PERF target unavailable",kind(error));
  require(same_target(target,expected)&&target.handle==id.nvapi_handle&&target.device_id==id.nvapi_devid&&target.subsystem_id==id.nvapi_subsys&&std::strcmp(target.slot,id.slot)==0,"Voltage evidence target differs from loaded equation target");
  if(!api.target.architecture_supported(target.architecture))fail("Voltage evidence architecture unsupported",Availability::unsupported);return target;
 }
 Bytes image_once(dc_gpu* gpu,const dc_target& target){
  binding(gpu,target);Bytes bytes(DC_VBIOS_IMAGE_CAPACITY);uint32_t size=0;dc_error error{};error.size=sizeof(error);
  if(api.image(gpu,bytes.data(),DC_VBIOS_IMAGE_CAPACITY,&size,&error)!=DC_OK)fail("Current ROM capture failed",kind(error));
  require(size>=32&&size<=DC_VBIOS_IMAGE_CAPACITY,"Current ROM capture length malformed");bytes.resize(size);binding(gpu,target);return bytes;
 }
 Bytes image(dc_gpu* gpu,const dc_target& target){
  for(unsigned attempt=0;attempt<3;++attempt){try{auto first=image_once(gpu,target),second=image_once(gpu,target);if(first==second)return second;
    if(attempt==2)fail("Current ROM differs between matching read pairs",Availability::transient);
   }catch(const VoltageEquationEvidenceError& error){if(error.kind!=Availability::transient||attempt==2)throw;}}
  fail("Current ROM stability unavailable",Availability::transient);
 }
 Availability rail_missing(const dc_rails_caches& cache,unsigned index,bool status_present,bool delta_present,bool raw_present,dc_gpu* gpu){
  const auto absent=[&](int getter,uint32_t export_id){if(!api.interface_present(gpu,export_id))return Availability::unsupported;
   const auto& d=cache.diagnostics[getter][index];if(!d.present)return Availability::transient;
   if(d.status==-3||d.status==-9||d.status==-104)return Availability::unsupported;
   if(d.status==0&&!d.valid)return Availability::invalid;return Availability::transient;};
  Availability a=Availability::unsupported;
  for(const auto family:{DC_RAILS_CONTROL,DC_RAILS_ABS}){if((family==DC_RAILS_ABS&&status_present)||(family==DC_RAILS_CONTROL&&delta_present&&raw_present))continue;
   const auto current=absent(family,family==DC_RAILS_ABS?DC_RAILS_ABS_ID:DC_RAILS_CONTROL_ID);if(current==Availability::invalid)return current;if(current==Availability::transient)a=current;}
  return a;
 }
 RailRound rails(dc_gpu* gpu,const dc_target& target){
  auto state=std::make_unique<dc_rails_state_set>();state->size=sizeof(*state);auto deltas=std::make_unique<dc_rails_delta_set>();deltas->size=sizeof(*deltas);
  auto blocks=std::make_unique<dc_rails_block_set>();blocks->size=sizeof(*blocks);auto caches=std::make_unique<dc_rails_caches>();caches->size=sizeof(*caches);
  binding(gpu,target);
  const auto checked=[](dc_status status){if(status!=DC_OK)fail("Independent rail policy/CONTROL capture failed",status==DC_E_UNAVAILABLE?Availability::unsupported:status==DC_E_FAILED?Availability::transient:Availability::invalid);};
  checked(api.state(gpu,state.get()));checked(api.deltas(gpu,deltas.get()));
  checked(api.blocks(gpu,DC_RAILS_CONTROL,DC_RAILS_CONTROL_VERSION,blocks.get()));checked(api.caches(gpu,caches.get()));
  require(state->size==sizeof(*state)&&deltas->size==sizeof(*deltas)&&blocks->size==sizeof(*blocks)&&caches->size==sizeof(*caches)&&!(state->present&~3u)&&!(deltas->present&~3u)&&!(blocks->present&~3u),"Rail policy/CONTROL ABI geometry malformed");
  RailRound out;
  for(unsigned i=0;i<2;++i){auto& row=out.rails[i];const uint32_t bit=1u<<i;
   if(!(state->present&bit)||!(deltas->present&bit)||!(blocks->present&bit)){row.availability=rail_missing(*caches,i,(state->present&bit)!=0,(deltas->present&bit)!=0,(blocks->present&bit)!=0,gpu);row.message="Independent selected rail policy or CONTROL unavailable";continue;}
   try{const auto& s=state->rails[i];const auto& d=deltas->rails[i];const auto* raw=blocks->blocks[i];const auto version=u32(raw);const size_t at=0x48+0x54*i;
    require(s.type==(i==0?1u:3u),"Rail policy discriminator outside understood positional semantics");
    require((version==0x20ac8||version==0x10ac8)&&u32(raw+4)==bit&&u32(raw+at)==static_cast<uint32_t>(d.type),"Raw rail CONTROL singleton/version/discriminator differs from policy");
    row.policy={microvolts(s.reliability),microvolts(s.alt_reliability),microvolts(s.overvoltage),microvolts(s.vmin),microvolts(s.effective)};
    for(auto value:row.policy)require(value>0,"Independent rail policy contains unavailable voltage");
    row.offsets={microvolts(d.reliability,true),microvolts(d.alt_reliability,true),microvolts(d.overvoltage,true),microvolts(d.vmin,true)};row.boost_uv=microvolts(s.boost_mv);
    for(size_t k=0;k<4;++k){int32_t current;const auto bits=u32(raw+at+4+4*k);std::memcpy(&current,&bits,4);require(current==row.offsets[k],"Raw rail CONTROL selected offsets differ from independent policy");}
    row.present=true;row.availability=Availability::available;
    append(out.identity,i);append(out.identity,s.type);append(out.identity,s.reserved);append(out.identity,static_cast<uint32_t>(d.type));append(out.identity,static_cast<uint32_t>(d.reserved));append(out.identity,version);
    append(out.requests,i);append(out.requests,0xac8);out.requests.insert(out.requests.end(),raw,raw+0xac8);
   }catch(const VoltageEquationEvidenceError& error){row=VoltageEquationRail{};row.availability=error.kind;row.message=error.what();}
  }
  binding(gpu,target);return out;
 }
 static bool same_rail(const VoltageEquationRail& a,const VoltageEquationRail& b){return a.present==b.present&&a.availability==b.availability&&(!a.present||(a.policy==b.policy&&a.offsets==b.offsets&&a.boost_uv==b.boost_uv));}
 RailRound stable_rails_once(dc_gpu* gpu,const dc_target& target){
  auto first=rails(gpu,target),second=rails(gpu,target);
  // A failed peer rail cannot erase another independently stable rail.
  for(size_t i=0;i<2;++i)if(!same_rail(first.rails[i],second.rails[i])){const bool invalid=first.rails[i].availability==Availability::invalid||second.rails[i].availability==Availability::invalid;
   second.rails[i]=VoltageEquationRail{};second.rails[i].availability=invalid?Availability::invalid:Availability::transient;second.rails[i].message=invalid?"Rail policy or CONTROL geometry malformed":"Rail policy changed between fresh reads";}
  // Rebuild receipts using only the independently stable present inventory.
  if(first.identity!=second.identity||first.requests!=second.requests){
   for(size_t i=0;i<2;++i)if(second.rails[i].present){
    // Packets are variable inventory records; compare the exact selected rail slice.
    const auto find=[](const Bytes& bytes,uint32_t rail,bool raw)->Bytes{size_t at=0;while(at<bytes.size()){const size_t length=raw?8+0xac8:24;if(at+length>bytes.size())fail("Malformed internal rail receipt");if(u32(bytes.data()+at)==rail)return Bytes(bytes.begin()+at,bytes.begin()+at+length);at+=length;}return {};};
    if(find(first.identity,static_cast<uint32_t>(i),false)!=find(second.identity,static_cast<uint32_t>(i),false)||find(first.requests,static_cast<uint32_t>(i),true)!=find(second.requests,static_cast<uint32_t>(i),true)){second.rails[i]=VoltageEquationRail{};second.rails[i].availability=Availability::transient;second.rails[i].message="Rail CONTROL changed between fresh reads";}
   }
  }
  Bytes identity,requests;for(size_t at=0;at<second.identity.size();at+=24){const auto rail=u32(second.identity.data()+at);if(second.rails[rail].present)identity.insert(identity.end(),second.identity.begin()+at,second.identity.begin()+at+24);}
  for(size_t at=0;at<second.requests.size();at+=8+0xac8){const auto rail=u32(second.requests.data()+at);if(second.rails[rail].present)requests.insert(requests.end(),second.requests.begin()+at,second.requests.begin()+at+8+0xac8);}
  second.identity=std::move(identity);second.requests=std::move(requests);return second;
 }
 RailRound stable_rails(dc_gpu* gpu,const dc_target& target){
  auto result=stable_rails_once(gpu,target);
  if(std::none_of(result.rails.begin(),result.rails.end(),[](const auto& rail){return rail.availability==Availability::invalid;})&&std::any_of(result.rails.begin(),result.rails.end(),[](const auto& rail){return rail.availability==Availability::transient;})){
   if(api.refresh)api.refresh(gpu);result=stable_rails_once(gpu,target);
  }
  return result;
 }
 void immutable_graph(const VbiosVoltageGraph& graph,const VoltageEquationPackets& packets){
  if(!graph.graph_complete||!graph.clock_roots_complete)fail("Complete ROM sharing and clock-root evidence unavailable",Availability::unsupported);
  for(uint32_t index=0;index<8192;++index){const bool active=index<graph.equations.size()&&graph.equations[index].kind!=0;
   require(present(packets.info,index)==active,"Loaded equation inventory differs from current ROM graph");if(!active)continue;
   const auto& rom=graph.equations[index];if(rom.kind!=1&&rom.kind!=7)fail("ROM equation runtime type translation is not independently qualified",Availability::unsupported);
   const auto* info=packets.info.data()+0x408+0x70*index;const auto* control=packets.control.data()+0x408+0x80*index;const uint8_t type=rom.kind==1?3:6;
   require(info[0]==type&&info[0x6c]==type&&control[0]==type&&control[0x7c]==type&&u16(info+2)==rom.next&&info[0x6d]==rom.variable&&info[0x6e]==rom.output,"Loaded equation graph identity differs from ROM");
   if(rom.kind==7){require(size_t(control[0x78])==rom.variables.size()&&size_t(control[0x79])==rom.terms.size(),"Loaded polynomial counts differ from ROM graph");
    const auto match=[&](const std::vector<VbiosVoltageTerm>& terms,size_t offset){for(size_t i=0;i<terms.size();++i){const auto* current=control+offset+8*i;const auto& expected=terms[i];require(current[0]==expected.kind,"Loaded graph polynomial descriptor type differs from ROM");const uint32_t value=current[0]==1?u32(current+4):current[0]==2?current[4]:u16(current+4);require(value==expected.value,"Loaded graph polynomial descriptor value differs from ROM");}};
    match(rom.variables,0x10);match(rom.terms,0x28);
   }else{
    // BIOS type1 is a runtime quadratic. Only an actual constant can carry
    // a prior live constant edit; all other coefficients remain ROM-bound.
    require(u32(control+0x14)==u32(rom.raw.data()+15)&&u32(control+0x18)==u32(rom.raw.data()+19),"Loaded quadratic coefficients differ from ROM");
    const bool constant=rom.lower==rom.upper&&rom.upper==rom.coefficients[0]&&rom.coefficients[1]==0&&rom.coefficients[2]==0;
    if(constant){const auto lo=f32(control+4),hi=f32(control+8),value=f32(control+0x10);require(std::isfinite(lo)&&lo==hi&&hi==value,"Loaded graph constant is malformed");}
    else require(u32(control+0x10)==u32(rom.raw.data()+11),"Loaded quadratic constant coefficient differs from ROM");
   }
  }
 }
 void loaded(const VbiosVoltageGraph& graph,const VbiosVoltageRoleBinding& role,const VoltageEquationPackets& packets,VoltageEquationRole& out){
  if(!role.encoding_qualified)fail("ROM role encoding or complete sharing graph is unqualified",Availability::unsupported);
  const auto index=role.equation;require(present(packets.info,index),"ROM role is absent from loaded equation inventory");const auto& rom=graph.equations[index];
  const auto* info=packets.info.data()+0x408+0x70*index;const auto* control=packets.control.data()+0x408+0x80*index;const bool rel=role.role==VbiosVoltageRole::rel;const uint8_t type=rel?6:3;
  require(info[0]==type&&info[0x6c]==type&&control[0]==type&&control[0x7c]==type&&info[0x6d]==rom.variable&&info[0x6e]==rom.output&&u16(info+2)==rom.next,"Loaded equation immutable type/next/variable/output differs from ROM");
  require(rom.next==0xffff||(rom.next!=index&&present(packets.info,rom.next)),"Loaded role links to itself or absent equation");
  const auto whole=[](float value){return std::isfinite(value)&&double(value)>=INT32_MIN&&double(value)<=INT32_MAX&&std::floor(value)==value;};
  const auto lo=f32(control+4),hi=f32(control+8);require(whole(lo)&&whole(hi)&&lo>=0&&hi>=lo,"Loaded equation bounds not canonical whole microvolts");
  if(rel){require(hi>0&&size_t(control[0x78])==rom.variables.size()&&size_t(control[0x79])==rom.terms.size(),"Loaded REL polynomial counts differ from ROM");
   const auto match=[&](const std::vector<VbiosVoltageTerm>& terms,size_t offset){for(size_t i=0;i<terms.size();++i){const auto* term=control+offset+8*i;const auto& expected=terms[i];require(term[0]==expected.kind,"Loaded REL polynomial term kind differs from ROM");
     const uint32_t value=term[0]==1?u32(term+4):term[0]==2?term[4]:u16(term+4);require(value==expected.value,"Loaded REL polynomial term value differs from ROM");
     if(term[0]==3)require(value!=index&&present(packets.info,value),"Loaded polynomial term is self-referencing or absent");}};
   match(rom.variables,0x10);match(rom.terms,0x28);
  }else require(lo==hi&&lo==f32(control+0x10)&&f32(control+0x14)==0&&f32(control+0x18)==0,"Loaded constant is not equal bounds/c0 with zero higher coefficients");
  out.info_row.assign(info,info+0x70);out.control_identity.assign(control,control+0x80);
  for(const auto offset:rel?std::vector<size_t>{4,8}:std::vector<size_t>{4,8,0x10})std::fill_n(out.control_identity.begin()+offset,4,uint8_t{0});
 }
 VoltageEquationEvidence once(dc_gpu* gpu,const VoltageEquationPackets& packets){
  if(!api.image||!api.sha256||!api.peers||!api.blocks||!api.state||!api.deltas||!api.caches||!api.interface_present)fail("Native voltage evidence dependency unavailable",Availability::unsupported);
  require(packets.info.size()==0xe0408&&packets.control.size()==0x100408,"Loaded equation packet geometry invalid");
  require(packets.info[0]==packets.control[0]&&std::equal(packets.info.begin()+4,packets.info.begin()+0x404,packets.control.begin()+4),"Loaded equation inventories differ");
  VoltageEquationEvidence result;result.target=binding(gpu,packets.target);auto bytes=image(gpu,result.target);const auto graph=parse_vbios_voltage_graph(bytes);
  if(!graph.ok)fail("Current ROM voltage inspection failed: "+graph.message,graph.message.find("Unrecognized")!=std::string::npos?Availability::unsupported:Availability::invalid);
  immutable_graph(graph,packets);
  char hash[65]{};if(api.sha256(bytes.data(),bytes.size(),hash)!=DC_OK)fail("Current ROM SHA256 unavailable",Availability::unsupported);
  require(hash[64]==0&&std::strlen(hash)==64&&std::string(hash).find_first_not_of("0123456789abcdef")==std::string::npos,"Current ROM SHA256 malformed");result.image_sha256=hash;
  auto peers=std::make_unique<dc_voltage_peers>();peers->size=sizeof(*peers);dc_error error{};error.size=sizeof(error);
  if(api.peers(gpu,peers.get(),&error)!=DC_OK)fail("Retained clock request capture failed",kind(error));require(peers->size==sizeof(*peers)&&same_target(peers->target,result.target),"Retained clock requests identify another target");
  const auto packet=[&](const dc_voltage_peer_packet& p,const uint8_t* raw,size_t capacity,bool computed){
   require(p.state<=DC_VOLTAGE_PEER_FAILED,"Retained peer packet has invalid state");if(p.state==DC_VOLTAGE_PEER_FAILED)fail("Present retained peer getter failed",Availability::transient);
   if(p.state==DC_VOLTAGE_PEER_ABSENT){require(p.length==0,"Absent retained packet has data");if(!computed)append(result.clock_requests,0);return;}
   require(p.length&&p.length<=capacity,"Retained peer packet length malformed");if(computed)result.computed_curve.assign(raw,raw+p.length);else{append(result.clock_requests,p.length);result.clock_requests.insert(result.clock_requests.end(),raw,raw+p.length);}};
  packet(peers->pstates,peers->pstates_bytes,DC_VOLTAGE_PEER_PSTATES_MAX,false);packet(peers->clock_control,peers->clock_bytes,DC_VOLTAGE_PEER_CLOCK_MAX,false);packet(peers->boost,peers->boost_bytes,DC_VOLTAGE_PEER_BOOST_SIZE,false);packet(peers->curve,peers->curve_bytes,DC_VOLTAGE_PEER_CURVE_SIZE,true);
  for(const auto value:{peers->clock_mask,peers->clock_version,peers->clock_header,peers->clock_stride,peers->clock_mask_dword,peers->clock_mode,peers->clock_freq_khz,peers->vf_entries})append(result.clock_requests,value);
  auto rails=stable_rails(gpu,result.target);result.rails=rails.rails;result.policy_identity=std::move(rails.identity);result.rail_requests=std::move(rails.requests);
  require(peers->voltage_boost.state<=DC_VOLTAGE_PEER_UNSUPPORTED,"Boost request has invalid availability");
  if(peers->voltage_boost.state==DC_VOLTAGE_PEER_FAILED)fail("Present voltage boost request getter failed",Availability::transient);
  if(peers->voltage_boost.state==DC_VOLTAGE_PEER_AVAILABLE){require(peers->voltage_boost.length==40&&peers->voltage_boost_percent>=0&&peers->voltage_boost_percent<=100,"Boost request geometry or percentage malformed");result.boost_percent=peers->voltage_boost_percent;}
  else require(peers->voltage_boost.length==0,"Absent/unsupported voltage boost request has data");
  append(result.clock_requests,peers->voltage_boost.state);append(result.clock_requests,static_cast<uint32_t>(peers->voltage_boost.status));
  for(const auto& role:graph.roles){if(role.role==VbiosVoltageRole::info_vmin)continue;VoltageEquationRole row;
   row.key={role.role==VbiosVoltageRole::rel?VoltageLimitKind::rel:role.role==VbiosVoltageRole::alt?VoltageLimitKind::alt:role.role==VbiosVoltageRole::ov?VoltageLimitKind::ov:VoltageLimitKind::vmin,role.rail,role.equation};
   row.graph_complete=role.graph_complete;row.shared=role.shared;row.affected_clock_roots=!role.affected_clock_roots.empty();
   if(role.stored_lower)row.stored_lower=*role.stored_lower;if(role.stored_value)row.stored_value=*role.stored_value;
   const auto& equation=graph.equations[role.equation];row.next=equation.next;row.variable=equation.variable;
   try{loaded(graph,role,packets,row);if(!result.rails[role.rail].present)fail(result.rails[role.rail].message,result.rails[role.rail].availability);
    if(role.role==VbiosVoltageRole::rel)for(const auto& root:role.affected_rail_roots){if(root.role!=VbiosVoltageRole::rel&&root.role!=VbiosVoltageRole::alt)fail("REL affects an unqualified rail selector",Availability::unsupported);
     require(root.equation<graph.equations.size(),"Affected rail root exceeds ROM graph");row.roots.push_back({root.rail,root.equation,root.role==VbiosVoltageRole::alt,graph.equations[root.equation].dependencies});
     if(!result.rails[root.rail].present)fail("Affected rail policy unavailable",result.rails[root.rail].availability);}
   }catch(const VoltageEquationEvidenceError& issue){row.availability=issue.kind;row.message=issue.what();}result.roles.push_back(std::move(row));
  }
  result.retained_requests_qualified=true;binding(gpu,result.target);return result;
 }
 VoltageEquationEvidence operator()(dc_gpu* gpu,const VoltageEquationPackets& packets){
  if(!api.target.lock||!api.target.unlock)fail("Voltage evidence lock interface unavailable",Availability::unsupported);
  if(api.target.lock(gpu)!=DC_OK)fail("Voltage evidence lock unavailable",Availability::transient);
  struct Unlock {VoltagePolicyApi& api;dc_gpu* gpu;~Unlock(){api.unlock(gpu);}} unlock{api.target,gpu};
  // Image/rail capture already owns bounded matching-read recovery, and the
  // service owns one whole-observation retry. Do not multiply those retries.
  return once(gpu,packets);
 }
};
}
std::function<VoltageEquationEvidence(dc_gpu*,const VoltageEquationPackets&)> make_voltage_equation_provider(VoltageEvidenceApi api,const dc_gpu_identity& target){
 auto provider=std::make_shared<Provider>(std::move(api),target);return [provider](dc_gpu* gpu,const VoltageEquationPackets& packets){return (*provider)(gpu,packets);};
}
}
