// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_limits_service.h"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <set>
#include <sstream>
#include <stdexcept>
namespace druta::ui {
bool operator==(const VoltageLimitKey& a,const VoltageLimitKey& b){return a.kind==b.kind&&a.rail==b.rail&&a.index==b.index;}
namespace {
constexpr size_t eq_header=0x408,eq_info_size=0xE0408,eq_control_size=0x100408,eq_info_stride=0x70,eq_control_stride=0x80;
constexpr uint32_t eq_info=0x2080A0B5,eq_control=0x2080A0B6,eq_set=0x2080E0B7,dev_info=0x2080B205,dev_control=0x2080B207,dev_set=0x2080F208;
using Bytes=std::vector<uint8_t>;using Availability=VoltagePolicyAvailability;
struct Error:std::runtime_error{Availability kind;Error(const std::string& s,Availability a):std::runtime_error(s),kind(a){}};
[[noreturn]]void fail(const std::string& message,Availability kind=Availability::invalid){throw Error(message,kind);}
Availability error_kind(const dc_error& error){return error.kind==DC_ERR_TRANSIENT||error.kind==DC_ERR_UNSTABLE?Availability::transient:error.kind==DC_ERR_UNSUPPORTED?Availability::unsupported:Availability::invalid;}
uint32_t u32(const Bytes& b,size_t at){if(at+4>b.size())fail("Voltage packet field is out of bounds");uint32_t v;std::memcpy(&v,b.data()+at,4);return v;}
uint32_t u16(const Bytes& b,size_t at){if(at+2>b.size())fail("Voltage packet field is out of bounds");uint16_t v;std::memcpy(&v,b.data()+at,2);return v;}
void put(Bytes& b,size_t at,uint32_t v){if(at+4>b.size())fail("Voltage projection is out of bounds");std::memcpy(b.data()+at,&v,4);}
int64_t uv(const Bytes& b,size_t at,bool positive=false){const auto bits=u32(b,at);float value;std::memcpy(&value,&bits,4);if(!std::isfinite(value)||std::floor(value)!=value||value<(positive?1.f:0.f)||double(value)>INT32_MAX)fail("Loaded voltage value is not exact whole float32 microvolts");return int64_t(value);}
void put_uv(Bytes& b,size_t at,int64_t value,bool positive=false){if(value<(positive?1:0)||value>INT32_MAX||int64_t(float(value))!=value)fail("Voltage request is not exactly representable in float32 microvolts");float f=float(value);uint32_t bits;std::memcpy(&bits,&f,4);put(b,at,bits);}
bool zero_float(const Bytes& b,size_t at){const auto bits=u32(b,at);float value;std::memcpy(&value,&bits,4);return value==0.f;}
bool term_present(const Bytes& b,uint32_t i){return i<8192&&(b[4+i/8]&(1u<<(i%8)));}
bool term_equal(const dc_target& a,const dc_target& b){return a.size==sizeof(a)&&b.size==sizeof(b)&&a.architecture==b.architecture&&a.device_id==b.device_id&&a.subsystem_id==b.subsystem_id&&a.handle==b.handle&&std::memcmp(a.slot,b.slot,sizeof(a.slot))==0;}
bool terminated(const char* p,size_t n){return std::memchr(p,0,n)!=nullptr;}
std::string hex(const Bytes& b){static constexpr char digits[]="0123456789abcdef";std::string out;out.reserve(b.size()*2);for(auto v:b){out+=digits[v>>4];out+=digits[v&15];}return out;}
const char* kind_name(VoltageLimitKind k){switch(k){case VoltageLimitKind::rel:return "REL bounds";case VoltageLimitKind::alt:return "ALT addend";case VoltageLimitKind::ov:return "OV constant";case VoltageLimitKind::vmin:return "VMIN constant";case VoltageLimitKind::switch_delay:return "Switch delay";}return "Unknown";}
Bytes identity_control(Bytes bytes,bool rel){for(size_t at:rel?std::vector<size_t>{4,8}:std::vector<size_t>{4,8,0x10})put(bytes,at,0);return bytes;}
struct Guard{VoltagePolicyApi& api;dc_gpu* gpu;bool held;Guard(VoltagePolicyApi& a,dc_gpu* g):api(a),gpu(g),held(a.lock&&a.unlock&&a.lock(g)==DC_OK){}~Guard(){if(held)api.unlock(gpu);}};
struct Capture{bool device=false;dc_target target{};Bytes info,control;dc_rm_transport transport{};std::optional<VoltageEquationEvidence> evidence;Availability evidence_availability=Availability::unsupported;std::string evidence_message;};
struct Record{VoltageLimitRequest request;int64_t before=0;std::optional<int64_t> before_lower;dc_rm_transport transport{};};
bool same_record(const Record& a,const Record& b){return a.request.key==b.request.key&&a.request.binding==b.request.binding&&a.request.value==b.request.value&&a.request.lower==b.request.lower&&a.before==b.before&&a.before_lower==b.before_lower;}
bool same_records(const std::vector<Record>& a,const std::vector<Record>& b){if(a.size()!=b.size())return false;for(size_t i=0;i<a.size();++i)if(!same_record(a[i],b[i]))return false;return true;}
std::string role_token(const VoltageEquationRole& r){std::ostringstream s;s<<int(r.key.kind)<<':'<<r.key.rail<<':'<<r.key.index<<':'<<r.next<<':'<<r.variable<<':'<<r.graph_complete<<':'<<r.shared<<':'<<r.affected_clock_roots<<':'<<r.stored_lower<<':'<<r.stored_value<<':'<<hex(r.info_row)<<':'<<hex(r.control_identity);for(const auto& root:r.roots){s<<'/'<<root.rail<<':'<<root.equation<<':'<<root.alt;for(auto i:root.dependencies)s<<','<<i;}return s.str();}
bool same_evidence(const VoltageEquationEvidence& a,const VoltageEquationEvidence& b){
 if(!term_equal(a.target,b.target)||a.image_sha256!=b.image_sha256||a.boost_percent!=b.boost_percent||a.policy_identity!=b.policy_identity||a.rail_requests!=b.rail_requests||a.clock_requests!=b.clock_requests||a.retained_requests_qualified!=b.retained_requests_qualified)return false;
 if(a.roles.size()!=b.roles.size())return false;for(size_t i=0;i<a.roles.size();++i)if(a.roles[i].availability!=b.roles[i].availability||role_token(a.roles[i])!=role_token(b.roles[i]))return false;
 for(size_t i=0;i<2;++i)if(a.rails[i].present!=b.rails[i].present||a.rails[i].availability!=b.rails[i].availability||a.rails[i].policy!=b.rails[i].policy||a.rails[i].offsets!=b.rails[i].offsets||a.rails[i].boost_uv!=b.rails[i].boost_uv)return false;
 return true; // evaluated curve is observed, not a stable request baseline
}
}
struct VoltageLimitsPlan::State {
 std::shared_ptr<int> session;dc_target target{};
 std::vector<VoltageLimitRequest> requests;
 std::vector<VoltageLimitRow> before;
};
struct VoltageLimitsReceipt::State {
 std::shared_ptr<int> session;dc_target target{};
 std::vector<Record> records,previous_undo,previous_completed;
 bool issued=false;
};
struct VoltageLimitsService::Impl {
 VoltageLimitsApi api;dc_gpu* gpu;dc_gpu_identity identity;std::optional<dc_target> sticky;VoltageLimitsData data;
 std::vector<Record> undo,completed;std::optional<Record> recovery;
 std::shared_ptr<int> session=std::make_shared<int>(0);
 Impl(VoltageLimitsApi a,dc_gpu* g,const dc_gpu_identity& id):api(std::move(a)),gpu(g),identity(id){data.identity=id;}
 dc_target bind(){auto& a=api.transport;if(!a.identity||!a.bind||!a.architecture_supported||!a.transport||!a.escape)fail("Paired voltage transport interfaces unavailable",Availability::unsupported);
  dc_gpu_identity now{};now.size=sizeof(now);if(a.identity(gpu,&now)!=DC_OK)fail("Selected adapter identity unavailable",Availability::transient);
  if(identity.size!=sizeof(identity)||now.size!=sizeof(now)||!terminated(identity.slot,sizeof(identity.slot))||!terminated(identity.uuid,sizeof(identity.uuid))||!terminated(now.slot,sizeof(now.slot))||!terminated(now.uuid,sizeof(now.uuid))||!now.nvapi_ok||!now.nvapi_selected||now.pairing_error[0]||std::strcmp(identity.slot,now.slot)||std::strcmp(identity.uuid,now.uuid)||identity.nvapi_handle!=now.nvapi_handle||identity.nvapi_devid!=now.nvapi_devid||identity.nvapi_subsys!=now.nvapi_subsys||identity.nvml_selected!=now.nvml_selected||identity.nvml_devid!=now.nvml_devid||identity.nvml_subsys!=now.nvml_subsys)fail("Voltage target identity or handle changed");
  dc_target target{};target.size=sizeof(target);dc_error error{};error.size=sizeof(error);if(a.bind(gpu,1,&target,&error)!=DC_OK)fail("Complete paired voltage target unavailable",error_kind(error));
  if(target.size!=sizeof(target)||!terminated(target.slot,sizeof(target.slot))||!target.slot[0]||!target.handle||std::strcmp(target.slot,identity.slot)||target.handle!=identity.nvapi_handle||target.device_id!=identity.nvapi_devid||target.subsystem_id!=identity.nvapi_subsys)fail("Voltage target does not match the current complete PCI binding");
  if(!a.architecture_supported(target.architecture))fail("Voltage layout architecture is unsupported",Availability::unsupported);if(sticky&&!term_equal(*sticky,target))fail("Voltage target architecture/layout binding changed");sticky=target;return target;
 }
 dc_rm_transport transport(){bind();dc_rm_transport t{};t.size=sizeof(t);dc_error error{};error.size=sizeof(error);if(api.transport.transport(gpu,&t,&error)!=DC_OK)fail("Voltage escape transport unavailable",error_kind(error));
  if(!t.captured||t.header_count!=17||uint64_t(t.header[2])!=68+uint64_t(t.header[15])||t.header[0]!=0x4E564441||t.header[1]!=0x10002||t.header[3]!=0x4E562A2A||t.header[4]!=0x0500002B)fail("Voltage escape envelope is not understood");bind();return t;
 }
 Bytes call(uint32_t command,size_t size,const dc_rm_transport& t,const Bytes* payload=nullptr,const Bytes* mask=nullptr,bool* dispatched=nullptr,const std::function<void()>& on_launch={}){
  std::vector<uint32_t> guarded((68+size)/4+32,0xA5A5A5A5);auto* p=guarded.data()+16;std::copy(t.header,t.header+17,p);p[2]=uint32_t(68+size);p[14]=command;p[15]=uint32_t(size);p[16]=0;p[5]=p[11]=0;auto* bytes=reinterpret_cast<uint8_t*>(p+17);
  if(payload){if(payload->size()!=size)fail("Voltage SET projection size changed");std::memcpy(bytes,payload->data(),size);}if(mask){if(mask->size()+4>size)fail("Voltage inventory is out of bounds");std::memcpy(bytes+4,mask->data(),mask->size());}
  std::array<uint32_t,16> expected;std::copy(p,p+16,expected.begin());bind();dc_error error{};error.size=sizeof(error);int32_t nt=0;if(on_launch)on_launch();if(dispatched)*dispatched=true;
  const auto status=api.transport.escape(gpu,reinterpret_cast<uint8_t*>(p),68+size,&t.fields,&nt,&error);if(dispatched&&status==DC_E_UNAVAILABLE)*dispatched=false;
  if(!std::all_of(guarded.begin(),guarded.begin()+16,[](uint32_t v){return v==0xA5A5A5A5;})||!std::all_of(guarded.end()-16,guarded.end(),[](uint32_t v){return v==0xA5A5A5A5;}))fail("Voltage packet exceeded bounds");bind();
  if(status!=DC_OK)fail("Voltage escape failed",error_kind(error));if(nt)fail("Voltage escape failed (NTSTATUS)",Availability::transient);
  for(size_t i=0;i<16;++i)if(i!=5&&i!=11&&p[i]!=expected[i])fail("Voltage response changed routing/geometry");if(p[5]>1)fail("Voltage context output is malformed");if(p[11]!=1)fail("Voltage escape has no completed response",Availability::transient);
  if(p[16])fail("Voltage RM request rejected: "+std::to_string(p[16]),p[16]==0x1F||p[16]==0x56?Availability::unsupported:Availability::transient);
  Bytes result(bytes,bytes+size);if(payload&&result!=*payload)fail("Voltage SET changed its caller-owned request");if(mask&&!std::equal(mask->begin(),mask->end(),result.begin()+4))fail("Voltage CONTROL changed its requested inventory");return result;
 }
 Capture once(bool device,const dc_rm_transport* fixed=nullptr,bool with_evidence=true){Capture c;c.device=device;c.target=bind();c.transport=fixed?*fixed:transport();
  if(!device&&(!api.interface_present||!api.interface_present(gpu,0x8D49471C)||!api.interface_present(gpu,0x4C75C9FE)))fail("VFE definition GET exports unavailable",Availability::unsupported);
  c.info=call(device?dev_info:eq_info,device?0x708:eq_info_size,c.transport);Bytes mask(c.info.begin()+4,c.info.begin()+(device?8:4+1024));
  c.control=call(device?dev_control:eq_control,device?0x188:eq_control_size,c.transport,nullptr,&mask);
  if(c.info[0]!=c.control[0]||!std::equal(mask.begin(),mask.end(),c.control.begin()+4))fail("Voltage INFO/CONTROL group or inventory disagrees");
  if(!device&&with_evidence&&api.equation_evidence){
   try{VoltageEquationPackets packets{c.target,c.info,c.control};c.evidence=api.equation_evidence(gpu,packets);if(!term_equal(c.evidence->target,c.target))fail("ROM/policy evidence belongs to a different voltage target");c.evidence_availability=Availability::available;}
   catch(const VoltageEquationEvidenceError& e){c.evidence_availability=e.kind;c.evidence_message=e.what();}
  }
  bind();return c;
 }
 Capture fresh(bool device,const dc_rm_transport* fixed=nullptr){for(int attempt=0;attempt<2;++attempt){try{const auto a=once(device,fixed);
   // Keep raw definition inspection independent of unavailable semantic
   // dependencies. A malformed/unsupported provider is not called again;
   // only transient provider failures receive the bounded read-only retry.
   auto b=once(device,fixed,a.evidence_message.empty());
   if(!a.evidence_message.empty()){b.evidence_availability=a.evidence_availability;b.evidence_message=a.evidence_message;}
   if(a.info!=b.info||a.control!=b.control)fail("Voltage definitions changed between fresh captures",Availability::transient);
   if(b.evidence_message.empty()&&(a.evidence.has_value()!=b.evidence.has_value()||(a.evidence&&!same_evidence(*a.evidence,*b.evidence))))fail("Voltage evidence changed between fresh captures",Availability::transient);
   if(!b.evidence&&b.evidence_availability==Availability::transient&&!attempt)fail(b.evidence_message,Availability::transient);
   return b;}
   catch(const Error& e){if(e.kind!=Availability::transient||attempt)throw;if(!fixed&&api.transport.reset_transport)api.transport.reset_transport(gpu,DC_TRANSPORT_POWER);}
   catch(const std::exception& e){if(attempt)fail(e.what(),Availability::transient);if(!fixed&&api.transport.reset_transport)api.transport.reset_transport(gpu,DC_TRANSPORT_POWER);}}
  fail("Bounded voltage read failed",Availability::transient);
 }
 std::vector<VoltageLimitRow> decode(const Capture& c){std::vector<VoltageLimitRow> rows;
  if(c.device){const auto mask=u32(c.info,4);data.device_count=0;for(uint32_t i=0;i<32;++i)if(mask&(1u<<i)){++data.device_count;VoltageLimitRow row;row.key={VoltageLimitKind::switch_delay,0,i};row.label="PWM device "+std::to_string(i)+" switch delay";const size_t at=8+i*56,ct=8+i*12;
    try{if(c.info[at]!=c.info[at+1]||c.info[at]!=c.control[ct]||c.info[at]!=c.control[ct+1])fail("Device type/duplicate type disagrees");if(c.info[at]!=3)fail("Only PWM type3 switch-delay semantics are qualified",Availability::unsupported);
     row.voltage_domain=c.info[at+2];row.i2c_device_index=c.info[at+3];row.operation_type=c.info[at+4];if(row.voltage_domain<1||row.voltage_domain>3||row.operation_type<1||row.operation_type>4)fail("Voltage domain or operation type is not understood",Availability::unsupported);
     if(u32(c.info,at+8)>u32(c.info,at+12)||!u32(c.control,ct+4))fail("Device voltage range or loaded positive delay is malformed");row.value=u32(c.control,ct+4);
     Bytes info(c.info.begin()+at,c.info.begin()+at+56),control(c.control.begin()+ct,c.control.begin()+ct+12);put(control,4,0);row.binding="device:"+std::to_string(i)+":"+hex(info)+":"+hex(control);row.availability=Availability::available;row.writable=true;
    }catch(const Error& e){row.availability=e.kind;row.message=e.what();}rows.push_back(std::move(row));}return rows;
  }
  data.equation_count=0;data.definitions.clear();for(uint32_t i=0;i<8192;++i)if(term_present(c.info,i)){++data.equation_count;VoltageLimitsData::Definition definition;definition.index=i;const size_t at=eq_header+i*eq_info_stride,ct=eq_header+i*eq_control_stride;definition.type=c.info[at];definition.next=u16(c.info,at+2);definition.variable=c.info[at+0x6D];
   try{if((definition.type!=3&&definition.type!=6)||c.info[at+0x6C]!=definition.type||c.control[ct]!=definition.type||c.control[ct+0x7C]!=definition.type||c.info[at+0x6E]!=2)fail("Definition type/output layout unqualified",Availability::unsupported);definition.lower=uv(c.control,ct+4);definition.value=uv(c.control,ct+8,definition.type==6);if(*definition.lower>*definition.value)fail("Definition bounds reversed");definition.message="Loaded definition; semantic ROM role unbound";}catch(const std::exception& e){definition.message=e.what();}data.definitions.push_back(std::move(definition));}
  if(!c.evidence){data.equations_message=c.evidence_message.empty()?"VFE definitions captured; native current-ROM role/graph and policy receipt binding unavailable":"VFE definitions captured; "+c.evidence_message;return rows;}
  const auto& evidence=*c.evidence;if(evidence.roles.size()>8)fail("Voltage role manifest has too many semantic roots");
  for(const auto& role:evidence.roles){VoltageLimitRow row;row.key=role.key;row.label=(role.key.rail==0?"NVVDD ":"MSVDD ")+std::string(kind_name(role.key.kind));
   try{if(std::count_if(evidence.roles.begin(),evidence.roles.end(),[&](const auto& r){return r.key==role.key;})!=1)fail("Duplicate voltage role identity");qualify(c,role,row);row.writable=true;row.availability=Availability::available;}catch(const Error& e){row.availability=e.kind;row.message=e.what();}rows.push_back(std::move(row));}
  return rows;
 }
 void qualify(const Capture& c,const VoltageEquationRole& role,VoltageLimitRow& row){const auto& ev=*c.evidence;const auto& key=role.key;const bool rel=key.kind==VoltageLimitKind::rel;
  if(role.availability!=Availability::available)fail(role.message.empty()?"Selected ROM role could not be qualified":role.message,role.availability);
  if(key.kind==VoltageLimitKind::switch_delay||key.rail>1||key.index>=8192||!role.graph_complete||role.affected_clock_roots||(!rel&&role.shared))fail("Equation role needs a complete, independent ROM graph binding",Availability::unsupported);
  if(!ev.rails[key.rail].present)fail(ev.rails[key.rail].message.empty()?"Independent selected rail policy binding unavailable":ev.rails[key.rail].message,ev.rails[key.rail].availability);
  if(ev.image_sha256.size()!=64||ev.image_sha256.find_first_not_of("0123456789abcdef")!=std::string::npos||!ev.retained_requests_qualified||ev.policy_identity.empty())fail("Current image or retained request binding unavailable",Availability::unsupported);
  if(ev.boost_percent&&(*ev.boost_percent<0||*ev.boost_percent>100))fail("Retained boost request is not a canonical percentage");
  for(const auto& rail:ev.rails)if(rail.present){for(auto value:rail.policy)if(value<1||uint64_t(value)>UINT32_MAX)fail("Reported policy is not canonical positive microvolts");for(auto offset:rail.offsets)if(offset<INT32_MIN||offset>INT32_MAX)fail("Retained policy offset is not signed microvolts");if(rail.boost_uv<0||uint64_t(rail.boost_uv)>UINT32_MAX)fail("Reported boost offset is malformed");}
  Bytes stored(4);put_uv(stored,0,role.stored_value,key.kind!=VoltageLimitKind::alt);if(rel){put_uv(stored,0,role.stored_lower);if(role.stored_lower>role.stored_value)fail("Stored ROM REL bounds are reversed");}
  if(!term_present(c.info,key.index))fail("Selected equation is absent",Availability::unsupported);const size_t at=eq_header+key.index*eq_info_stride,ct=eq_header+key.index*eq_control_stride;const uint8_t type=rel?6:3;
  if(c.info[at]!=type||c.info[at+0x6C]!=type||c.control[ct]!=type||c.control[ct+0x7C]!=type||c.info[at+0x6E]!=2)fail("Equation type/duplicate/output-unit geometry is not understood");
  if(u16(c.info,at+2)!=role.next||c.info[at+0x6D]!=role.variable||role.variable>255)fail("Loaded equation next/variable differs from ROM binding");
  if(role.next!=0xFFFF&&(role.next==key.index||!term_present(c.info,role.next)))fail("Selected equation links to itself or an absent equation");
  Bytes info(c.info.begin()+at,c.info.begin()+at+eq_info_stride),control(c.control.begin()+ct,c.control.begin()+ct+eq_control_stride);
  if(role.info_row!=info||role.control_identity!=identity_control(control,rel))fail("Loaded equation immutable terms differ from the current ROM role binding");
  if(rel){row.lower=uv(c.control,ct+4);row.value=uv(c.control,ct+8,true);if(*row.lower>*row.value)fail("REL bounds are reversed");if(!ev.boost_percent)fail("REL requires readable boost request",Availability::unsupported);
   const auto variables=c.control[ct+0x78],coefficients=c.control[ct+0x79];if(variables<1||variables>3||coefficients!=(variables==1?3:variables==2?6:10))fail("REL polynomial geometry is not understood");
   for(auto pair:{std::pair<size_t,uint8_t>{0x10,variables},{0x28,coefficients}})for(size_t i=0;i<pair.second;++i){const auto p=ct+pair.first+8*i;const auto type_id=c.control[p];if(type_id<1||type_id>3)fail("Unknown REL polynomial descriptor");const auto value=type_id==1?u32(c.control,p+4):type_id==2?c.control[p+4]:u16(c.control,p+4);if(type_id==1){float f;std::memcpy(&f,&value,4);if(!std::isfinite(f))fail("REL polynomial literal is not finite");}if(type_id==2&&value>254)fail("REL polynomial variable index invalid");if(type_id==3&&(value==key.index||!term_present(c.info,value)))fail("REL polynomial dependency is absent/self-referencing");}
   bool own=false;std::set<std::pair<uint32_t,bool>> seen;for(const auto& root:role.roots){if(root.rail>1||!ev.rails[root.rail].present||root.equation>=8192||!seen.insert({root.rail,root.alt}).second||!std::is_sorted(root.dependencies.begin(),root.dependencies.end())||std::adjacent_find(root.dependencies.begin(),root.dependencies.end())!=root.dependencies.end()||std::find(root.dependencies.begin(),root.dependencies.end(),key.index)==root.dependencies.end()||std::find(root.dependencies.begin(),root.dependencies.end(),root.equation)==root.dependencies.end())fail("REL affected rail-root manifest is incomplete");for(auto index:root.dependencies)if(!term_present(c.info,index))fail("REL root dependency is absent");own=own||(root.rail==key.rail&&!root.alt&&root.equation==key.index);}if(!own)fail("REL graph omits its selected own root");
  }else{row.value=uv(c.control,ct+4,key.kind!=VoltageLimitKind::alt);if(*row.value!=uv(c.control,ct+8)||*row.value!=uv(c.control,ct+0x10)||!zero_float(c.control,ct+0x14)||!zero_float(c.control,ct+0x18))fail("Constant bounds/c0 or higher coefficients are not an exact constant");
   if(key.kind==VoltageLimitKind::alt){if(!ev.boost_percent||role.next>=8192)fail("ALT requires a linked REL and boost request",Availability::unsupported);bool linked=false;for(const auto& other:ev.roles)linked=linked||(other.key.kind==VoltageLimitKind::rel&&other.key.rail==key.rail&&other.key.index==role.next);if(!linked)fail("ALT is not linked to the selected rail's REL equation");}else if(role.next!=0xFFFF)fail("OV/VMIN requires a terminal constant");
  }
  row.stored_lower=rel?std::optional<int64_t>(role.stored_lower):std::nullopt;row.stored_value=role.stored_value;
  row.reported=ev.rails[key.rail].policy[key.kind==VoltageLimitKind::rel?0:key.kind==VoltageLimitKind::alt?1:key.kind==VoltageLimitKind::ov?2:3];
  row.binding="equation:"+ev.image_sha256+":"+role_token(role);
 }
 void publish(const Capture& c){auto rows=decode(c);data.target=c.target;bool retained=false;for(auto& row:rows){const auto old=std::find_if(data.rows.begin(),data.rows.end(),[&](const auto& r){return r.key==row.key;});
    if(row.writable&&row.value){const bool same=old!=data.rows.end()&&old->binding==row.binding;row.initial_value=same?old->initial_value:row.value;row.initial_lower=same?old->initial_lower:row.lower;}
    else if(old!=data.rows.end()&&old->value){const auto message=row.message;const auto availability=row.availability;row=*old;row.stale=true;row.writable=false;row.message=message;row.availability=availability;retained=true;}}
  for(const auto& old:data.rows)if((old.key.kind==VoltageLimitKind::switch_delay)==c.device&&std::none_of(rows.begin(),rows.end(),[&](const auto& r){return r.key==old.key;})){
    auto stale=old;stale.stale=true;stale.writable=false;stale.availability=c.device?Availability::unsupported:(!c.evidence_message.empty()?c.evidence_availability:Availability::transient);
    stale.message=c.device?"Device absent from current inventory; retained last-good reading":(!c.evidence_message.empty()?c.evidence_message+"; retained last-good reading":"Current ROM role evidence did not reconfirm this control; retained last-good reading");rows.push_back(std::move(stale));retained=true;}
  data.rows.erase(std::remove_if(data.rows.begin(),data.rows.end(),[&](const auto& r){return (r.key.kind==VoltageLimitKind::switch_delay)==c.device;}),data.rows.end());data.rows.insert(data.rows.end(),rows.begin(),rows.end());
  if(c.device)data.devices_message="Read "+std::to_string(data.device_count)+" voltage devices; only qualified PWM delays are writable";
  else {data.definitions_stale=false;if(c.evidence)data.equations_message="Read "+std::to_string(data.equation_count)+" loaded equations with current ROM role evidence";}
  if(retained){if(c.device)data.devices_message+="; unavailable established readings retained stale";else data.equations_message+="; unavailable established readings retained stale";}
 }
 void unavailable(bool device,Availability availability,const std::string& message){for(auto& row:data.rows)if((row.key.kind==VoltageLimitKind::switch_delay)==device){row.stale=true;row.availability=availability;row.message=message;if(availability!=Availability::transient)row.writable=false;}
  if(device)data.devices_message=message;else {data.equations_message=message;data.definitions_stale=!data.definitions.empty();}
 }
 void flags(){data.undo_available=!undo.empty()||!completed.empty();data.recovery_pending=recovery.has_value();++data.revision;}
 VoltageLimitRow row(const Capture& c,const VoltageLimitKey& key){const auto rows=decode(c);const VoltageLimitRow* found=nullptr;for(const auto& r:rows)if(r.key==key){if(found)fail("Selected voltage identity is ambiguous");found=&r;}
  if(!found||!found->value||!found->writable)fail(found?found->message:"Selected voltage control is unavailable",found?found->availability:Availability::unsupported);return *found;
 }
 void validate(const Capture& c,const VoltageLimitRequest& request){const auto current=row(c,request.key);if(request.binding.empty()||current.binding!=request.binding)fail("Voltage selected geometry/role binding changed");
  if(request.expected_value&&request.expected_value!=current.value)fail("Selected voltage value changed since the draft",Availability::transient);if(request.lower&&request.expected_lower&&request.expected_lower!=current.lower)fail("Selected REL lower bound changed since the draft",Availability::transient);
  if(c.device){if(request.lower||request.key.rail||request.value<1||uint64_t(request.value)>UINT32_MAX)fail("Switch delay requires a positive exact uint32 microsecond request");return;}
  Bytes value(4);put_uv(value,0,request.value,request.key.kind!=VoltageLimitKind::alt);if(request.key.kind==VoltageLimitKind::rel){const auto low=request.lower.value_or(*current.lower);put_uv(value,0,low);if(low>request.value)fail("REL lower bound exceeds upper bound");}
  else if(request.lower)fail("Only REL exposes a lower bound");
  const auto& rail=c.evidence->rails[request.key.rail];for(size_t i=0;i<rail.policy.size();++i)if(rail.policy[i]<1||uint64_t(rail.policy[i])>UINT32_MAX)fail("Reported policy values are not exact positive microvolts");for(auto offset:rail.offsets)if(offset<INT32_MIN||offset>INT32_MAX)fail("Retained policy offset is not signed whole microvolts");
  if(request.key.kind==VoltageLimitKind::ov||request.key.kind==VoltageLimitKind::vmin){const size_t field=request.key.kind==VoltageLimitKind::ov?2:3;if(*current.value+rail.offsets[field]!=rail.policy[field])fail("Loaded constant plus retained offset differs from independent reported policy");const auto wanted=request.value+rail.offsets[field];if(wanted<1||uint64_t(wanted)>UINT32_MAX)fail("Constant would produce an invalid reported policy request");if(field==3)for(size_t i=0;i<5;++i)if(i!=3&&wanted>rail.policy[i])fail("VMIN exceeds another independently reported bound");}
  if(request.key.kind==VoltageLimitKind::alt){const auto wanted=rail.policy[1]+request.value-*current.value;if(wanted<1||uint64_t(wanted)>UINT32_MAX)fail("ALT addend would produce an invalid reported limit");}
 }
 void verify_policy(const Capture& before,const Capture& after,const VoltageLimitRequest& request,int64_t old_value){if(before.device)return;const auto& a=*before.evidence;const auto& b=*after.evidence;
  if(!term_equal(a.target,b.target)||a.image_sha256!=b.image_sha256||a.boost_percent!=b.boost_percent||a.policy_identity!=b.policy_identity||a.rail_requests!=b.rail_requests||a.clock_requests!=b.clock_requests||!b.retained_requests_qualified)fail("Voltage write changed image identity, retained requests or unknown policy fields");
  std::array<std::array<bool,5>,2> dynamic{};std::array<bool,2> dynamic_boost{};const auto kind=request.key.kind;
  if(kind==VoltageLimitKind::rel){const auto role=std::find_if(a.roles.begin(),a.roles.end(),[&](const auto& r){return r.key==request.key;});if(role==a.roles.end())fail("REL root manifest disappeared");for(const auto& root:role->roots){dynamic[root.rail][root.alt?1:0]=true;dynamic[root.rail][4]=true;}
   if(a.boost_percent.value_or(0)!=0)for(size_t rail=0;rail<2;++rail){dynamic[rail][0]=dynamic[rail][4]=true;dynamic_boost[rail]=true;}}
  if(kind==VoltageLimitKind::alt)for(size_t rail=0;rail<2;++rail){dynamic[rail][0]=dynamic[rail][4]=true;dynamic_boost[rail]=true;}
  if(kind==VoltageLimitKind::ov)dynamic[request.key.rail][4]=true;
  for(size_t rail=0;rail<2;++rail){if(a.rails[rail].present!=b.rails[rail].present||a.rails[rail].offsets!=b.rails[rail].offsets)fail("Independent rail inventory or retained offsets changed");if(!a.rails[rail].present)continue;if(!dynamic_boost[rail]&&a.rails[rail].boost_uv!=b.rails[rail].boost_uv)fail("Unselected reported boost changed");
   for(size_t field=0;field<5;++field){if(dynamic[rail][field])continue;int64_t expected=a.rails[rail].policy[field];if(rail==request.key.rail){if(kind==VoltageLimitKind::alt&&field==1)expected+=request.value-old_value;else if(kind==VoltageLimitKind::ov&&field==2)expected=request.value+a.rails[rail].offsets[2];else if(kind==VoltageLimitKind::vmin&&field==3)expected=request.value+a.rails[rail].offsets[3];}if(b.rails[rail].policy[field]!=expected)fail("Selected or preserved independently reported rail policy differs");}
  }
 }
 Bytes project(const Capture& before,const VoltageLimitRequest& request){Bytes projected=before.control;
  if(before.device)put(projected,8+request.key.index*12+4,uint32_t(request.value));
  else{const size_t at=eq_header+request.key.index*eq_control_stride;if(request.key.kind==VoltageLimitKind::rel){if(request.lower)put_uv(projected,at+4,*request.lower);put_uv(projected,at+8,request.value,true);}else for(size_t offset:{size_t{4},size_t{8},size_t{0x10}})put_uv(projected,at+offset,request.value,request.key.kind!=VoltageLimitKind::alt);}
  return projected;
 }
 Capture write(const Capture& before,const VoltageLimitRequest& request,bool& dispatched,const std::function<void()>& on_launch={}){validate(before,request);const auto original=row(before,request.key);const auto projected=project(before,request);auto payload=projected;
  if(before.device)put(payload,4,1u<<request.key.index);else{std::fill(payload.begin()+4,payload.begin()+4+1024,uint8_t{0});payload[4+request.key.index/8]=uint8_t(1u<<(request.key.index%8));}
  call(before.device?dev_set:eq_set,payload.size(),before.transport,&payload,nullptr,&dispatched,on_launch);
  const auto after=fresh(before.device,&before.transport);if(after.info!=before.info||after.control!=projected)fail("Voltage selected SET did not preserve the exact complete fresh projection");
  if(row(after,request.key).binding!=request.binding)fail("Selected semantic voltage binding changed during SET");
  verify_policy(before,after,request,*original.value);publish(after);return after;
 }
 bool repair(Record& record,std::string& message,bool fixed=false){try{const bool device=record.request.key.kind==VoltageLimitKind::switch_delay;const auto current=fresh(device,fixed?&record.transport:nullptr);const auto selected=row(current,record.request.key);
    if(selected.binding!=record.request.binding)fail("Selected voltage geometry changed; restoration refused");if(*selected.value!=record.before&&*selected.value!=record.request.value)fail("Selected voltage value changed outside this edit; restoration refused");
    if(record.before_lower&&selected.lower!=record.before_lower&&selected.lower!=record.request.lower)fail("Selected REL lower bound changed outside this edit");
    if(*selected.value==record.before&&(!record.before_lower||selected.lower==record.before_lower)){publish(current);return true;}
    VoltageLimitRequest restore=record.request;restore.value=record.before;restore.lower=record.before_lower;restore.expected_value=selected.value;restore.expected_lower=selected.lower;bool attempted=false;
    try{write(current,restore,attempted);return true;}catch(...){if(!attempted)throw;const auto after=fresh(device,&current.transport);if(after.info!=current.info||after.control!=project(current,restore))throw;verify_policy(current,after,restore,*selected.value);publish(after);return true;}
   }catch(const std::exception& e){message=e.what();return false;}}
 Capture sample(bool device){try{auto captured=fresh(device);publish(captured);return captured;}catch(const Error& e){unavailable(device,e.kind,e.what());throw;}catch(const std::exception& e){unavailable(device,Availability::transient,e.what());throw;}}
 dc_target checked_bind(bool device,bool equation){try{return bind();}catch(const Error& e){if(device)unavailable(true,e.kind,e.what());if(equation)unavailable(false,e.kind,e.what());throw;}catch(const std::exception& e){if(device)unavailable(true,Availability::transient,e.what());if(equation)unavailable(false,Availability::transient,e.what());throw;}}
 VoltageLimitsPlan prepare(const std::vector<VoltageLimitRequest>& requests){
  if(recovery)fail("Recover pending selected voltage requests before profile preflight");if(requests.empty()||requests.size()>40)fail("Select between one and forty voltage controls");
  auto state=std::make_shared<VoltageLimitsPlan::State>();state->session=session;std::optional<Capture> device,equation;std::vector<VoltageLimitKey> seen;
  for(const auto& request:requests){const auto& key=request.key;const bool dev=key.kind==VoltageLimitKind::switch_delay;if(static_cast<uint32_t>(key.kind)>4||key.rail>1||key.index>=(dev?32u:8192u)||(dev&&key.rail))fail("Invalid selected voltage key");for(const auto& old:seen)if(old==key||(!dev&&old.kind!=VoltageLimitKind::switch_delay&&old.index==key.index))fail("Duplicate or aliased selected voltage control");seen.push_back(key);
   auto& capture=dev?device:equation;if(!capture)capture=sample(dev);validate(*capture,request);auto original=row(*capture,key);auto frozen=request;frozen.expected_value=original.value;if(frozen.lower)frozen.expected_lower=original.lower;state->requests.push_back(std::move(frozen));state->before.push_back(std::move(original));
  }
  state->target=checked_bind(device.has_value(),equation.has_value());VoltageLimitsPlan plan;plan.ok=true;plan.message="All selected voltage controls freshly preflighted; no writes";plan.state=std::move(state);return plan;
 }
 const VoltageLimitsPlan::State& qualify(const VoltageLimitsPlan& plan){if(!plan.ok||!plan.state||plan.state->session!=session||plan.state->requests.empty()||plan.state->requests.size()!=plan.state->before.size())fail("Voltage plan belongs to another session or is invalid");bool device=false,equation=false;for(const auto& r:plan.state->requests)(r.key.kind==VoltageLimitKind::switch_delay?device:equation)=true;if(!term_equal(plan.state->target,checked_bind(device,equation)))fail("Voltage plan target changed");return *plan.state;}
 VoltageLimitsReceipt::State& qualify(const VoltageLimitsReceipt& receipt){if(!receipt.state||receipt.state->session!=session||!receipt.state->issued||receipt.state->records.empty())fail("Voltage receipt belongs to another session or was not issued");bool device=false,equation=false;for(const auto& r:receipt.state->records)(r.request.key.kind==VoltageLimitKind::switch_delay?device:equation)=true;if(!term_equal(receipt.state->target,checked_bind(device,equation)))fail("Voltage receipt target changed");return *receipt.state;}
 void check_selected(const std::vector<VoltageLimitRequest>& requests,bool validate_requests=false){std::optional<Capture> device,equation;for(const auto& request:requests){auto& capture=request.key.kind==VoltageLimitKind::switch_delay?device:equation;if(!capture)capture=sample(request.key.kind==VoltageLimitKind::switch_delay);if(validate_requests)validate(*capture,request);else{const auto current=row(*capture,request.key);if(current.binding!=request.binding||current.value!=request.value||(request.lower&&current.lower!=request.lower))fail("Final selected voltage profile readback differs");}}}
 VoltageLimitsResult apply(const std::vector<VoltageLimitRequest>& requests){VoltageLimitsResult result;Guard guard(api.transport,gpu);if(!guard.held){result.message="GPU lock unavailable";return result;}if(recovery){result.message="Recover the pending selected voltage request first";return result;}
  std::vector<Record> issued;const auto prior=undo,prior_completed=completed;bool started=false;
  const auto read_fresh=[&](bool device){try{auto capture=fresh(device);publish(capture);return capture;}catch(const Error& e){unavailable(device,e.kind,e.what());throw;}catch(const std::exception& e){unavailable(device,Availability::transient,e.what());throw;}};
  try{if(requests.empty()||requests.size()>40)fail("Select between one and forty understood voltage controls");std::optional<Capture> equations,devices;std::vector<VoltageLimitKey> seen;
   for(const auto& request:requests){const bool device=request.key.kind==VoltageLimitKind::switch_delay;for(const auto& key:seen)if(key==request.key||(!device&&key.kind!=VoltageLimitKind::switch_delay&&key.index==request.key.index))fail("Duplicate or aliased selected voltage equation/device");seen.push_back(request.key);
    auto& capture=device?devices:equations;if(!capture)capture=read_fresh(device);validate(*capture,request);}
   for(const auto& request:requests){const bool device=request.key.kind==VoltageLimitKind::switch_delay;const auto before=read_fresh(device);validate(before,request);const auto selected=row(before,request.key);
    if(selected.value==request.value&&(!request.lower||selected.lower==request.lower)){publish(before);continue;}
    Record record{request,*selected.value,request.lower?selected.lower:std::nullopt,before.transport};bool dispatched=false;
    try{write(before,request,dispatched);started=true;issued.push_back(record);result.may_have_changed=true;}
    catch(...){result.may_have_changed=result.may_have_changed||dispatched;if(dispatched){started=true;std::string reason;result.rollback_verified=repair(record,reason,true);if(!result.rollback_verified){recovery=record;unavailable(device,Availability::transient,"Selected write unconfirmed; recovery pending: "+reason);}}throw;}
   }
   // Re-read every selected final value after all selected writes.
   for(const auto& request:requests){const auto current=read_fresh(request.key.kind==VoltageLimitKind::switch_delay);const auto selected=row(current,request.key);if(selected.binding!=request.binding||selected.value!=request.value||(request.lower&&selected.lower!=request.lower))fail("Final selected voltage request differs after another selected write");}
   result.ok=true;result.message="Selected voltage requests read back exactly; physical voltage/latency effects are not measured";
  }catch(const std::exception& e){result.message=e.what();if(recovery)result.message+="; typed selected recovery pending";else if(result.rollback_verified)result.message+="; failed selected request restored and verified";}
  if(started&&!issued.empty()){undo=std::move(issued);completed.clear();}else{undo=prior;completed=prior_completed;}data.message=result.message;flags();return result;
 }
};
VoltageLimitsService::VoltageLimitsService(VoltageLimitsApi api,dc_gpu* gpu,const dc_gpu_identity& identity):impl_(std::make_unique<Impl>(std::move(api),gpu,identity)){}
VoltageLimitsService::~VoltageLimitsService()=default;
const VoltageLimitsData& VoltageLimitsService::state()const{return impl_->data;}
const VoltageLimitsData& VoltageLimitsService::read(uint32_t families){auto& p=*impl_;Guard guard(p.api.transport,p.gpu);if(!families||(families&~3u)){p.data.message="Select understood voltage families";p.flags();return p.data;}
 for(bool device:{false,true})if(families&(device?2u:1u)){try{if(!guard.held)fail("GPU lock unavailable",Availability::transient);p.publish(p.fresh(device));}catch(const Error& e){p.unavailable(device,e.kind,e.what());}catch(const std::exception& e){p.unavailable(device,Availability::transient,e.what());}}
 p.data.message="Voltage inspection refreshed; evaluate each family's independent status";p.flags();return p.data;
}
VoltageLimitsResult VoltageLimitsService::apply(const std::vector<VoltageLimitRequest>& requests){return impl_->apply(requests);}
VoltageLimitsPlan VoltageLimitsService::preview(const std::vector<VoltageLimitRequest>& requests){auto& p=*impl_;VoltageLimitsPlan result;Guard guard(p.api.transport,p.gpu);try{if(!guard.held)fail("GPU lock unavailable");result=p.prepare(requests);}catch(const std::exception& e){result.message=e.what();}p.data.message=result.message;p.flags();return result;}
VoltageLimitsResult VoltageLimitsService::apply_profile(const VoltageLimitsPlan& plan,const std::function<void(const VoltageLimitsReceipt&)>& journal){
 auto& p=*impl_;VoltageLimitsResult result;Guard guard(p.api.transport,p.gpu);VoltageLimitsReceipt receipt;
 try{if(!guard.held)fail("GPU lock unavailable");const auto& prepared=p.qualify(plan);if(p.recovery)fail("Recover pending voltage requests before profile Apply");p.check_selected(prepared.requests,true);
  const auto old_undo=p.undo,old_completed=p.completed;
  for(const auto& request:prepared.requests){auto guarded=prepared.requests;if(receipt.state)for(const auto& issued:receipt.state->records)for(auto& entry:guarded)if(entry.key==issued.request.key){entry.expected_value=issued.request.value;if(entry.lower)entry.expected_lower=issued.request.lower;}p.check_selected(guarded,true);
   const bool device=request.key.kind==VoltageLimitKind::switch_delay;const auto current=p.sample(device);p.validate(current,guarded[static_cast<size_t>(&request-prepared.requests.data())]);const auto selected=p.row(current,request.key);if(selected.value==request.value&&(!request.lower||selected.lower==request.lower))continue;
   Record record{request,*selected.value,request.lower?selected.lower:std::nullopt,current.transport};bool dispatched=false,added=false;
   try{p.write(current,request,dispatched,[&]{if(!receipt.state){receipt.state=std::make_shared<VoltageLimitsReceipt::State>();receipt.state->session=p.session;receipt.state->target=prepared.target;receipt.state->previous_undo=old_undo;receipt.state->previous_completed=old_completed;}auto& state=*receipt.state;state.records.push_back(record);added=true;state.issued=true;if(journal)journal(receipt);p.undo=state.records;p.completed.clear();});}
   catch(...){if(added&&!dispatched){receipt.state->records.pop_back();receipt.state->issued=!receipt.state->records.empty();p.undo=receipt.state->records.empty()?old_undo:receipt.state->records;p.completed=receipt.state->records.empty()?old_completed:std::vector<Record>{};}if(dispatched){p.recovery=record;p.unavailable(device,Availability::transient,"Selected catalog write unconfirmed; typed recovery retained");}throw;}
  }
  p.check_selected(prepared.requests);result.ok=true;result.message="All selected voltage profile requests verified; physical effects are not measured";
 }catch(const std::exception& e){result.message=std::string(e.what())+(receipt.state&&receipt.state->issued?"; issued selected catalog receipt retained":"; no selected catalog write issued");}
 result.may_have_changed=receipt.state&&receipt.state->issued;p.data.message=result.message;p.flags();return result;
}
VoltageLimitsResult VoltageLimitsService::verify(const VoltageLimitsPlan& plan){auto& p=*impl_;VoltageLimitsResult result;Guard guard(p.api.transport,p.gpu);try{if(!guard.held)fail("GPU lock unavailable");const auto& prepared=p.qualify(plan);p.check_selected(prepared.requests);result.ok=true;result.message="Entire selected voltage plan verified";}catch(const std::exception& e){result.message=e.what();}p.data.message=result.message;p.flags();return result;}
VoltageLimitsResult VoltageLimitsService::verify(const VoltageLimitsReceipt& receipt,bool original){auto& p=*impl_;VoltageLimitsResult result;Guard guard(p.api.transport,p.gpu);try{if(!guard.held)fail("GPU lock unavailable");const auto& state=p.qualify(receipt);std::vector<VoltageLimitRequest> requests;for(const auto& record:state.records){auto request=record.request;if(original){request.value=record.before;request.lower=record.before_lower;}requests.push_back(std::move(request));}p.check_selected(requests);result.ok=true;result.message=original?"Original issued voltage requests verified":"Issued desired voltage requests verified";}catch(const std::exception& e){result.message=e.what();}p.data.message=result.message;p.flags();return result;}
VoltageLimitsResult VoltageLimitsService::restore(const VoltageLimitsReceipt& receipt){
 auto& p=*impl_;VoltageLimitsResult result;Guard guard(p.api.transport,p.gpu);
 try{if(!guard.held)fail("GPU lock unavailable");auto& state=p.qualify(receipt);if(p.recovery&&std::none_of(state.records.begin(),state.records.end(),[&](const auto& r){return same_record(r,*p.recovery);}))fail("A different voltage recovery is pending");
  // Validate every selected candidate before the first restore. repair() also
  // rechecks each immediately before projecting onto fresh unknown fields.
  for(const auto& record:state.records){const auto current=p.sample(record.request.key.kind==VoltageLimitKind::switch_delay);const auto selected=p.row(current,record.request.key);if(selected.binding!=record.request.binding||(selected.value!=record.before&&selected.value!=record.request.value)||(record.before_lower&&selected.lower!=record.before_lower&&selected.lower!=record.request.lower))fail("Selected voltage receipt was replaced; restoration refused");}
  const bool replace_undo=same_records(p.undo,state.records);for(auto it=state.records.rbegin();it!=state.records.rend();++it){std::string reason;result.may_have_changed=true;if(!p.repair(*it,reason)){p.recovery=*it;p.unavailable(it->request.key.kind==VoltageLimitKind::switch_delay,Availability::transient,reason);fail(reason);}}
  std::vector<VoltageLimitRequest> originals;for(const auto& record:state.records){auto request=record.request;request.value=record.before;request.lower=record.before_lower;originals.push_back(std::move(request));}p.check_selected(originals);
  if(replace_undo){p.undo=state.previous_undo;p.completed=state.previous_completed;}if(p.recovery&&std::any_of(state.records.begin(),state.records.end(),[&](const auto& r){return same_record(r,*p.recovery);}))p.recovery.reset();result.ok=result.rollback_verified=true;result.message="Issued selected voltage requests restored; prior standalone Undo preserved";
 }catch(const std::exception& e){result.message=std::string(e.what())+"; catalog receipt retained";}
 p.data.message=result.message;p.flags();return result;
}
VoltageLimitsResult VoltageLimitsService::restore_initial(const VoltageLimitKey& key){for(const auto& r:impl_->data.rows)if(r.key==key&&r.initial_value)return apply(VoltageLimitRequest{key,*r.initial_value,r.initial_lower,r.binding,r.value,r.lower});return {false,false,false,"Qualified captured initial request unavailable"};}
VoltageLimitsResult VoltageLimitsService::restore_rom(const VoltageLimitKey& key){for(const auto& r:impl_->data.rows)if(r.key==key&&r.stored_value)return apply(VoltageLimitRequest{key,*r.stored_value,r.stored_lower,r.binding,r.value,r.lower});return {false,false,false,"Current-ROM qualified bounds/constant unavailable"};}
VoltageLimitsResult VoltageLimitsService::recover(){auto& p=*impl_;VoltageLimitsResult out;Guard guard(p.api.transport,p.gpu);if(!guard.held){out.message="GPU lock unavailable";return out;}if(!p.recovery){out.ok=true;out.message="No selected recovery pending";return out;}std::string error;out.ok=p.repair(*p.recovery,error);out.may_have_changed=true;out.rollback_verified=out.ok;if(out.ok){p.recovery.reset();out.message="Selected prior request restored and verified; current unselected fields preserved";}else{out.message="Selected recovery pending: "+error;p.unavailable(p.recovery->request.key.kind==VoltageLimitKind::switch_delay,Availability::transient,out.message);}p.data.message=out.message;p.flags();return out;}
VoltageLimitsResult VoltageLimitsService::undo(){auto& p=*impl_;if(p.recovery)return recover();VoltageLimitsResult out;Guard guard(p.api.transport,p.gpu);if(!guard.held){out.message="GPU lock unavailable";return out;}if(p.undo.empty()&&p.completed.empty()){out.message="No voltage-limits Undo";return out;}
 while(!p.undo.empty()){std::string error;out.may_have_changed=true;if(!p.repair(p.undo.back(),error)){out.message="Voltage Undo retained: "+error;p.data.message=out.message;p.flags();return out;}p.completed.push_back(std::move(p.undo.back()));p.undo.pop_back();}
 for(auto it=p.completed.begin();it!=p.completed.end();){bool match=false;try{const auto current=p.fresh(it->request.key.kind==VoltageLimitKind::switch_delay);const auto selected=p.row(current,it->request.key);match=selected.binding==it->request.binding&&selected.value==it->before&&(!it->before_lower||selected.lower==it->before_lower);}catch(...){match=false;}if(!match){p.undo.push_back(std::move(*it));it=p.completed.erase(it);}else ++it;}
 out.ok=p.undo.empty();out.message=out.ok?"Original selected voltage requests verified":"Final selected Undo readback differs/unavailable; typed recovery retained";if(out.ok)p.completed.clear();p.data.message=out.message;p.flags();return out;
}
namespace {
void profile_keys(const ProfileJson& j,std::initializer_list<const char*> fields){if(!j.is_object()||j.size()!=fields.size())fail("Voltage profile fields are missing/unexpected");for(auto key:fields)if(!j.contains(key))fail("Voltage profile field missing");}
int64_t profile_int(const ProfileJson& j){if(!j.is_number_integer()||(j.is_number_unsigned()&&j.get<uint64_t>()>uint64_t(INT64_MAX)))fail("Voltage profile needs an exact signed-range integer");return j.get<int64_t>();}
template<size_t N>std::string id_text(const char(&s)[N]){const auto* end=static_cast<const char*>(std::memchr(s,0,N));if(!end||end==s)fail("Voltage profile target identity unavailable");return std::string(s,end);}
const VoltageLimitRow& profile_row(const VoltageLimitsData& data,const VoltageLimitKey& key){const VoltageLimitRow* out=nullptr;for(const auto& row:data.rows)if(row.key==key){if(out)fail("Duplicate current voltage identity");out=&row;}if(!out||!out->value||out->binding.empty()||out->stale||out->availability!=Availability::available||!out->writable)fail("Fresh qualified selected voltage request unavailable");return *out;}
void unique_keys(const std::vector<VoltageLimitKey>& selection){std::vector<VoltageLimitKey> seen;for(const auto& key:selection){if(key.rail>1||key.index>=(key.kind==VoltageLimitKind::switch_delay?32u:8192u)||(key.kind==VoltageLimitKind::switch_delay&&key.rail))fail("Voltage profile key is outside the understood inventory");for(const auto& old:seen)if(old==key||(key.kind!=VoltageLimitKind::switch_delay&&old.kind!=VoltageLimitKind::switch_delay&&key.index==old.index))fail("Voltage profile contains duplicate/aliased targets");seen.push_back(key);}}
}
ProfileJson capture_voltage_limits_profile(const VoltageLimitsData& data,const std::vector<VoltageLimitKey>& selection){if(selection.empty()||selection.size()>40)fail("Select qualified voltage controls to save");unique_keys(selection);ProfileJson entries=ProfileJson::array();for(const auto& key:selection){const auto& row=profile_row(data,key);ProfileJson e={{"kind",int(key.kind)},{"rail",key.rail},{"index",key.index},{"binding",row.binding},{"unit",key.kind==VoltageLimitKind::switch_delay?"us":"uV"},{"value",*row.value}};if(key.kind==VoltageLimitKind::rel){if(!row.lower)fail("REL lower bound unavailable");e["lower"]=*row.lower;}entries.push_back(std::move(e));}
 return {{"format","druta-native-voltage-limits"},{"version",1},{"target",{{"uuid",id_text(data.identity.uuid)},{"slot",id_text(data.identity.slot)}}},{"entries",std::move(entries)}};
}

namespace {
void limit_keys(const ProfileJson& d,std::initializer_list<const char*> required,std::initializer_list<const char*> optional={}){if(!d.is_object())fail("Voltage profile object required");for(auto key:required)if(!d.contains(key))fail("Voltage profile field missing");for(auto it=d.begin();it!=d.end();++it)if(std::find(required.begin(),required.end(),it.key())==required.end()&&std::find(optional.begin(),optional.end(),it.key())==optional.end())fail("Voltage profile has unrelated fields");}
std::string limit_text(const ProfileJson& j,bool empty=false){if(!j.is_string())fail("Voltage profile string required");auto s=j.get<std::string>();if((!empty&&s.empty())||s.size()>profile_max_bytes||s.find('\0')!=std::string::npos)fail("Voltage profile string malformed");return s;}
uint32_t limit_u32(const ProfileJson& j,uint32_t maximum=UINT32_MAX){const auto value=profile_int(j);if(value<0||uint64_t(value)>maximum)fail("Voltage profile integer outside range");return static_cast<uint32_t>(value);}
std::string limit_target(const VoltageLimitKey& key){return key.kind==VoltageLimitKind::switch_delay?"voltage:delay:"+std::to_string(key.index):"voltage:equation:"+std::to_string(static_cast<uint32_t>(key.kind))+":"+std::to_string(key.rail)+":"+std::to_string(key.index);}
struct LimitEntry {VoltageLimitRequest request;uint32_t domain=0,operation=0,i2c=0;};
struct LimitProfile {ProfileSelection selection;bool python=false;std::optional<std::array<uint32_t,3>> adapter;std::vector<LimitEntry> entries;};
LimitProfile parse_limit_profile(const ProfileJson& doc){LimitProfile p;std::vector<VoltageLimitKey> keys;
 const auto add=[&](LimitEntry entry){keys.push_back(entry.request.key);unique_keys(keys);p.selection.targets.push_back(limit_target(entry.request.key));p.entries.push_back(std::move(entry));};
 if(doc.contains("format")){profile_keys(doc,{"format","version","target","entries"});if(doc["format"]!="druta-native-voltage-limits"||profile_int(doc["version"])!=1)fail("Unsupported voltage profile format");profile_keys(doc["target"],{"uuid","slot"});p.selection.uuid=limit_text(doc["target"]["uuid"]);p.selection.slot=limit_text(doc["target"]["slot"]);
  if(!doc["entries"].is_array()||doc["entries"].empty()||doc["entries"].size()>40)fail("Invalid voltage profile entries");for(const auto& e:doc["entries"]){LimitEntry entry;auto& r=entry.request;r.key={static_cast<VoltageLimitKind>(limit_u32(e.at("kind"),4)),limit_u32(e.at("rail"),1),limit_u32(e.at("index"),8191)};if(r.key.kind==VoltageLimitKind::rel)profile_keys(e,{"kind","rail","index","binding","unit","value","lower"});else profile_keys(e,{"kind","rail","index","binding","unit","value"});r.binding=limit_text(e["binding"]);if(e["unit"]!=(r.key.kind==VoltageLimitKind::switch_delay?"us":"uV"))fail("Voltage profile units differ");r.value=profile_int(e["value"]);if(r.key.kind==VoltageLimitKind::rel)r.lower=profile_int(e["lower"]);Bytes value(4);if(r.key.kind==VoltageLimitKind::switch_delay){if(r.value<1||uint64_t(r.value)>UINT32_MAX)fail("Invalid positive uint32 switch delay");}else{put_uv(value,0,r.value,r.key.kind!=VoltageLimitKind::alt);if(r.lower){put_uv(value,0,*r.lower);if(*r.lower>r.value)fail("Profile REL bounds reversed");}}add(std::move(entry));}
 }else{p.python=true;limit_keys(doc,{"schema","scope","voltage_controls"},{"saved_at","saved_ts","device","incomplete"});if(profile_int(doc["schema"])!=2||doc["scope"]!="voltage_controls")fail("Unsupported Python voltage control scope");if(doc.contains("incomplete")&&(!doc["incomplete"].is_array()||!doc["incomplete"].empty()))fail("Incomplete voltage control profile");if(doc.contains("saved_at")&&!doc["saved_at"].is_string())fail("Invalid voltage profile timestamp");if(doc.contains("saved_ts")&&(!doc["saved_ts"].is_number()||doc["saved_ts"].is_boolean()||!std::isfinite(doc["saved_ts"].get<double>())))fail("Invalid voltage profile timestamp");
  const auto& payload=doc["voltage_controls"];profile_keys(payload,{"kind","identity","rows"});if(payload["kind"]!="switch_delay")fail("Only switch_delay Python controls belong to this service import");const auto& identity=payload["identity"];profile_keys(identity,{"slot","architecture","device_id","subsystem_id"});p.selection.slot=limit_text(identity["slot"]);p.adapter=std::array<uint32_t,3>{limit_u32(identity["architecture"]),limit_u32(identity["device_id"],65535),limit_u32(identity["subsystem_id"])};
  if(doc.contains("device")){const auto& device=doc["device"];limit_keys(device,{}, {"name","vbios","driver","uuid","slot"});for(auto it=device.begin();it!=device.end();++it)if(!it.value().is_null())limit_text(it.value(),true);if(device.contains("uuid")&&!device["uuid"].is_null())p.selection.uuid=limit_text(device["uuid"],true);if(device.contains("slot")&&!device["slot"].is_null()&&limit_text(device["slot"])!=p.selection.slot)fail("Conflicting saved adapter slots");}
  const auto& rows=payload["rows"];if(!rows.is_array()||rows.empty()||rows.size()>32)fail("Invalid switch delay rows");for(const auto& e:rows){profile_keys(e,{"index","type","voltage_domain","operation_type","i2c_device_index","switch_delay_us"});LimitEntry entry;entry.request.key={VoltageLimitKind::switch_delay,0,limit_u32(e["index"],31)};if(limit_u32(e["type"],255)!=3)fail("Only PWM type3 delays are understood");entry.domain=limit_u32(e["voltage_domain"],3);entry.operation=limit_u32(e["operation_type"],4);entry.i2c=limit_u32(e["i2c_device_index"],255);entry.request.value=limit_u32(e["switch_delay_us"]);if(!entry.domain||!entry.operation||!entry.request.value)fail("Delay domain/operation/value must be positive");add(std::move(entry));}
 }return p;
}
}
ProfileSelection describe_voltage_limits_profile(const ProfileJson& doc){return parse_limit_profile(doc).selection;}
ProfileJson select_voltage_limits_profile(const ProfileJson& doc,const std::vector<std::string>& targets){const auto parsed=parse_limit_profile(doc);if(targets.empty())fail("Select voltage controls");std::vector<std::string> seen;for(const auto& t:targets){if(std::find(parsed.selection.targets.begin(),parsed.selection.targets.end(),t)==parsed.selection.targets.end()||std::find(seen.begin(),seen.end(),t)!=seen.end())fail("Unknown or duplicate selected voltage target");seen.push_back(t);}auto out=doc;ProfileJson selected=ProfileJson::array();const auto& entries=parsed.python?doc["voltage_controls"]["rows"]:doc["entries"];for(size_t i=0;i<parsed.entries.size();++i)if(std::find(seen.begin(),seen.end(),parsed.selection.targets[i])!=seen.end())selected.push_back(entries[i]);if(parsed.python)out["voltage_controls"]["rows"]=std::move(selected);else out["entries"]=std::move(selected);(void)parse_limit_profile(out);return out;}
std::vector<VoltageLimitRequest> stage_voltage_limits_profile(const ProfileJson& doc,const VoltageLimitsData& data){const auto parsed=parse_limit_profile(doc);if(!terminated(data.identity.slot,sizeof(data.identity.slot))||!terminated(data.identity.uuid,sizeof(data.identity.uuid))||parsed.selection.slot!=data.identity.slot||(!parsed.selection.uuid.empty()&&parsed.selection.uuid!=data.identity.uuid))fail("Voltage profile adapter identity differs");
 if(parsed.adapter&&(!data.target||data.target->size!=sizeof(dc_target)||(*parsed.adapter)[0]!=data.target->architecture||(*parsed.adapter)[1]!=data.target->device_id||(*parsed.adapter)[2]!=data.target->subsystem_id))fail("Saved complete voltage adapter identity changed");
 std::vector<VoltageLimitRequest> result;for(const auto& entry:parsed.entries){const auto& row=profile_row(data,entry.request.key);auto request=entry.request;if(!parsed.python&&request.binding!=row.binding)fail("Voltage profile current binding changed");if(parsed.python&&(row.voltage_domain!=entry.domain||row.operation_type!=entry.operation||row.i2c_device_index!=entry.i2c))fail("Switch-delay current domain, operation or I2C identity changed");request.binding=row.binding;request.expected_value=row.value;request.expected_lower=row.lower;result.push_back(std::move(request));}return result;
}
}
