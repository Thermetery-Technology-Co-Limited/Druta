// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_limits_service.h"
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
template<class F>static void rejects(F f){bool rejected=false;try{f();}catch(const std::exception&){rejected=true;}CHECK(rejected);}
namespace {
using Bytes=std::vector<uint8_t>;dc_gpu_identity device{};Bytes di,dc,ei,ec;bool equations=false,foreign=false,foreign_after=false,nt_after=false,drift_after=false,no_dispatch=false,bad_graph=false,omit_ov=false;
int failed_gets=0,block_after=0,sets=0,resets=0,transport_calls=0,binding_calls=0,bind_error=0,transport_error=0,catalog_fail_at=0;
uint32_t get(const Bytes& b,size_t at){uint32_t n;std::memcpy(&n,b.data()+at,4);return n;}void put(Bytes& b,size_t at,uint32_t n){std::memcpy(b.data()+at,&n,4);}void volts(Bytes& b,size_t at,uint32_t n){float f=float(n);std::memcpy(b.data()+at,&f,4);}int64_t voltage(const Bytes& b,size_t at){float f;std::memcpy(&f,b.data()+at,4);return int64_t(f);}
dc_status DC_CALL id(const dc_gpu*,dc_gpu_identity* out){*out=device;if(foreign)++out->nvapi_devid;return DC_OK;}
dc_status DC_CALL lock(dc_gpu*){return DC_OK;}dc_status DC_CALL unlock(dc_gpu*){return DC_OK;}
int32_t DC_CALL arch(uint32_t value){return value>=4&&value<=10;}
dc_status DC_CALL bind(dc_gpu*,int32_t complete,dc_target* out,dc_error* error){++binding_calls;CHECK(complete==1);if(bind_error){error->kind=bind_error;bind_error=0;return DC_E_FAILED;}out->size=sizeof(*out);std::strcpy(out->slot,device.slot);out->architecture=7;out->device_id=device.nvapi_devid;out->subsystem_id=device.nvapi_subsys;out->handle=device.nvapi_handle;return DC_OK;}
dc_status DC_CALL transport(dc_gpu*,dc_rm_transport* out,dc_error* error){++transport_calls;if(transport_error){error->kind=transport_error;transport_error=0;return DC_E_FAILED;}out->captured=1;out->header_count=17;out->header[0]=0x4E564441;out->header[1]=0x10002;out->header[2]=76;out->header[3]=0x4E562A2A;out->header[4]=0x0500002B;out->header[15]=8;return DC_OK;}
dc_status DC_CALL reset(dc_gpu*,int32_t kind){CHECK(kind==DC_TRANSPORT_POWER);++resets;return DC_OK;}
int32_t DC_CALL exported(const dc_gpu*,uint32_t value){CHECK(value==0x8D49471C||value==0x4C75C9FE);return equations?1:0;}
dc_status DC_CALL escape(dc_gpu*,uint8_t* raw,size_t size,const dc_escape_fields*,int32_t* nt,dc_error*){auto* p=reinterpret_cast<uint32_t*>(raw);CHECK(p[2]==size&&p[15]+68==size&&p[5]==0&&p[11]==0);const auto command=p[14];const bool setting=command==0x2080F208||command==0x2080E0B7;*nt=0;
 if(!setting&&failed_gets){--failed_gets;*nt=-1;return DC_OK;}if(setting&&no_dispatch){no_dispatch=false;return DC_E_UNAVAILABLE;}p[5]=p[11]=1;p[16]=0;auto* bytes=reinterpret_cast<uint8_t*>(p+17);
 if(command==0x2080B205){CHECK(p[15]==di.size());std::memcpy(bytes,di.data(),di.size());}
 else if(command==0x2080B207){CHECK(p[15]==dc.size()&&p[18]==get(di,4));std::memcpy(bytes,dc.data(),dc.size());}
 else if(command==0x2080F208){++sets;const auto mask=p[18];CHECK(mask&&!(mask&(mask-1)));for(uint32_t i=0;i<32;++i)if(mask&(1u<<i)){const auto at=8+i*12;for(size_t b=0;b<12;++b)if(b<4||b>=8)CHECK(bytes[at+b]==dc[at+b]);std::memcpy(dc.data()+at+4,bytes+at+4,4);}if(drift_after){dc[2]^=0x17;drift_after=false;}}
 else if(command==0x2080A0B5){CHECK(equations&&p[15]==ei.size());std::memcpy(bytes,ei.data(),ei.size());}
 else if(command==0x2080A0B6){CHECK(p[15]==ec.size()&&std::equal(ei.begin()+4,ei.begin()+1028,bytes+4));std::memcpy(bytes,ec.data(),ec.size());}
 else {CHECK(command==0x2080E0B7&&p[15]==ec.size());++sets;unsigned count=0;for(uint32_t i=0;i<8192;++i)if(bytes[4+i/8]&(1u<<(i%8))){++count;const auto at=0x408+i*0x80;std::memcpy(ec.data()+at,bytes+at,0x80);}CHECK(count==1);}
 if(setting){if(foreign_after){foreign_after=false;foreign=true;}if(nt_after){nt_after=false;*nt=-1;}if(catalog_fail_at==sets)*nt=-1;if(block_after){failed_gets=block_after;block_after=0;}}
 return DC_OK;
}
void fresh(){device={};device.size=sizeof(device);std::strcpy(device.slot,"0000:09:00.0");std::strcpy(device.uuid,"GPU-alternate-limits");device.nvapi_ok=device.nvapi_selected=1;device.nvapi_handle=42;device.nvapi_devid=0x4567;device.nvapi_subsys=0xAABBCCDD;
 equations=foreign=foreign_after=nt_after=drift_after=no_dispatch=bad_graph=omit_ov=false;failed_gets=block_after=sets=resets=transport_calls=binding_calls=bind_error=transport_error=catalog_fail_at=0;
 di.assign(0x708,0xA5);dc.assign(0x188,0xA5);di[0]=dc[0]=2;const auto mask=(1u<<1)|(1u<<7)|(1u<<12);put(di,4,mask);put(dc,4,mask);
 for(uint32_t i:{1u,7u,12u}){const size_t at=8+i*56,ct=8+i*12;di[at]=di[at+1]=dc[ct]=dc[ct+1]=i==7?4:3;di[at+2]=i==12?3:1;di[at+3]=5;di[at+4]=i==12?4:1;put(di,at+8,700000);put(di,at+12,1300000);put(di,at+16,1000);put(dc,ct+4,i==12?40:20);put(dc,ct+8,0xC0FFEE);}
 ei.assign(0xE0408,0xA5);ec.assign(0x100408,0xA5);ei[0]=ec[0]=3;std::fill(ei.begin()+4,ei.begin()+1028,0);std::fill(ec.begin()+4,ec.begin()+1028,0);
 for(uint32_t i:{3u,4u,5u,6u}){ei[4+i/8]|=uint8_t(1u<<(i%8));ec[4+i/8]|=uint8_t(1u<<(i%8));const auto at=0x408+i*0x70,ct=0x408+i*0x80;const bool rel=i==6;ei[at]=ei[at+0x6C]=ec[ct]=ec[ct+0x7C]=rel?6:3;uint16_t next=i==4?6:0xFFFF;std::memcpy(ei.data()+at+2,&next,2);ei[at+0x6D]=7;ei[at+0x6E]=2;
  const uint32_t value=i==3?1200000:i==4?10000:i==5?700000:1100000;volts(ec,ct+4,rel?0:value);volts(ec,ct+8,value);
  if(rel){ec[ct+0x78]=1;ec[ct+0x79]=3;ec[ct+0x10]=2;ec[ct+0x14]=1;for(size_t j=0;j<3;++j){ec[ct+0x28+8*j]=1;volts(ec,ct+0x2C+8*j,1);}}
  else{volts(ec,ct+0x10,value);volts(ec,ct+0x14,0);volts(ec,ct+0x18,0);}
 }
}
VoltageEquationEvidence evidence(dc_gpu*,const VoltageEquationPackets& packets){VoltageEquationEvidence out;out.target=packets.target;out.image_sha256=std::string(64,'b');out.retained_requests_qualified=true;out.policy_identity={1,3,5};out.clock_requests={2,4};out.rail_requests={6,8};out.boost_percent=0;out.rails[0].present=true;out.rails[0].policy={1100000,1110000+voltage(packets.control,0x408+4*0x80+4),voltage(packets.control,0x408+3*0x80+4),voltage(packets.control,0x408+5*0x80+4),1100000};
 for(auto pair:{std::pair<VoltageLimitKind,uint32_t>{VoltageLimitKind::ov,3},{VoltageLimitKind::alt,4},{VoltageLimitKind::vmin,5},{VoltageLimitKind::rel,6}}){VoltageEquationRole role;role.key={pair.first,0,pair.second};role.graph_complete=!(bad_graph&&pair.first==VoltageLimitKind::ov);role.next=pair.first==VoltageLimitKind::alt?6:0xFFFF;role.variable=7;role.stored_value=pair.first==VoltageLimitKind::ov?1200000:pair.first==VoltageLimitKind::alt?10000:pair.first==VoltageLimitKind::vmin?700000:1100000;
  const auto at=0x408+pair.second*0x70,ct=0x408+pair.second*0x80;role.info_row.assign(packets.info.begin()+at,packets.info.begin()+at+0x70);role.control_identity.assign(packets.control.begin()+ct,packets.control.begin()+ct+0x80);put(role.control_identity,4,0);put(role.control_identity,8,0);if(pair.first!=VoltageLimitKind::rel)put(role.control_identity,0x10,0);else role.roots={{0,6,false,{6}},{0,4,true,{4,6}}};out.roles.push_back(std::move(role));
 }if(omit_ov)out.roles.erase(out.roles.begin());return out;
}
VoltageLimitsApi api(bool provider=false){VoltageLimitsApi a;a.transport={id,bind,arch,transport,escape,reset,lock,unlock};a.interface_present=exported;if(provider)a.equation_evidence=evidence;return a;}
VoltageLimitRequest request(const VoltageLimitsService& s,VoltageLimitKind kind,uint32_t index,int64_t value){for(const auto& row:s.state().rows)if(row.key.kind==kind&&row.key.index==index)return {row.key,value,{},row.binding,row.value,row.lower};CHECK(false);return {};}
}

static ProfileJson python_delays(const VoltageLimitsData& data){const auto& t=*data.target;return {{"schema",2},{"scope","voltage_controls"},{"saved_at","2026-11-13"},{"saved_ts",2.0},{"device",{{"uuid",data.identity.uuid},{"slot",data.identity.slot}}},{"incomplete",ProfileJson::array()},{"voltage_controls",{{"kind","switch_delay"},{"identity",{{"slot",data.identity.slot},{"architecture",t.architecture},{"device_id",t.device_id},{"subsystem_id",t.subsystem_id}}},{"rows",ProfileJson::array({{{"index",1},{"type",3},{"voltage_domain",1},{"operation_type",1},{"i2c_device_index",5},{"switch_delay_us",350}},{{"index",12},{"type",3},{"voltage_domain",3},{"operation_type",4},{"i2c_device_index",5},{"switch_delay_us",UINT32_MAX}}})}}}};}
static void exchange_profiles(){fresh();std::strcpy(device.uuid,"GPU-delay-import-alternate");device.nvapi_devid=0x1234;VoltageLimitsService service(api(),reinterpret_cast<dc_gpu*>(1),device);const auto data=service.read(2);const auto document=python_delays(data);const auto reads=transport_calls;
 auto description=describe_voltage_limits_profile(document);CHECK(description.targets==std::vector<std::string>({"voltage:delay:1","voltage:delay:12"})&&description.uuid==device.uuid&&description.slot==device.slot);
 const auto staged=stage_voltage_limits_profile(document,data);CHECK(staged.size()==2&&staged[0].value==350&&staged[0].expected_value==20&&staged[1].value==UINT32_MAX&&sets==0&&transport_calls==reads);
 const auto subset=select_voltage_limits_profile(document,{"voltage:delay:12"});CHECK(subset["voltage_controls"]["rows"].size()==1&&stage_voltage_limits_profile(subset,data).size()==1&&describe_voltage_limits_profile(subset).targets==std::vector<std::string>({"voltage:delay:12"}));
 rejects([&]{select_voltage_limits_profile(document,{"voltage:delay:1","voltage:delay:1"});});rejects([&]{select_voltage_limits_profile(document,{"voltage:delay:7"});});
 auto bad=document;bad["voltage_controls"]["identity"]["architecture"]=8;rejects([&]{stage_voltage_limits_profile(bad,data);});bad=document;bad["voltage_controls"]["identity"]["device_id"]=0x4321;rejects([&]{stage_voltage_limits_profile(bad,data);});bad=document;bad["voltage_controls"]["identity"]["subsystem_id"]=1;rejects([&]{stage_voltage_limits_profile(bad,data);});bad=document;bad["device"]["uuid"]="GPU-other";rejects([&]{stage_voltage_limits_profile(bad,data);});
 bad=document;bad["voltage_controls"]["rows"][0]["i2c_device_index"]=6;rejects([&]{stage_voltage_limits_profile(bad,data);});bad=document;bad["voltage_controls"]["rows"][0]["voltage_domain"]=2;rejects([&]{stage_voltage_limits_profile(bad,data);});bad=document;bad["voltage_controls"]["rows"][0]["operation_type"]=2;rejects([&]{stage_voltage_limits_profile(bad,data);});
 for(const auto name:{"type","index","switch_delay_us"}){bad=document;bad["voltage_controls"]["rows"][0][name]=true;rejects([&]{describe_voltage_limits_profile(bad);});}
 bad=document;bad["voltage_controls"]["rows"][0]["switch_delay_us"]=uint64_t(UINT32_MAX)+1;rejects([&]{describe_voltage_limits_profile(bad);});bad=document;bad["voltage_controls"]["rows"][0]["switch_delay_us"]=0;rejects([&]{describe_voltage_limits_profile(bad);});bad=document;bad["voltage_controls"]["rows"][0]["switch_delay_us"]=35.0;rejects([&]{describe_voltage_limits_profile(bad);});
 bad=document;bad["voltage_controls"]["kind"]="gate";rejects([&]{describe_voltage_limits_profile(bad);});bad=document;bad["voltage_controls"]["rows"].push_back(bad["voltage_controls"]["rows"][0]);rejects([&]{describe_voltage_limits_profile(bad);});bad=document;bad["packet"]="opaque";rejects([&]{describe_voltage_limits_profile(bad);});bad=document;bad["incomplete"]={"missing device"};rejects([&]{describe_voltage_limits_profile(bad);});
 // Independent unsupported index7 and absent semantic equation provider do
 // not suppress selected PWM delay imports.
 CHECK(!data.rows[1].writable&&stage_voltage_limits_profile(document,data).size()==2&&sets==0);failed_gets=2;service.read(2);rejects([&]{stage_voltage_limits_profile(document,service.state());});service.read(2);CHECK(stage_voltage_limits_profile(document,service.state()).size()==2);
 di[8+1*56+3]=9;service.read(2);rejects([&]{stage_voltage_limits_profile(document,service.state());});
 di[8+1*56+3]=5;service.read(2);CHECK(service.apply(stage_voltage_limits_profile(document,service.state())).ok&&get(dc,24)==350&&get(dc,8+12*12+4)==UINT32_MAX);CHECK(service.undo().ok&&get(dc,24)==20&&get(dc,8+12*12+4)==40);
 fresh();equations=true;VoltageLimitsService qualified(api(true),reinterpret_cast<dc_gpu*>(1),device);qualified.read();const auto native=capture_voltage_limits_profile(qualified.state(),{{VoltageLimitKind::switch_delay,0,1},{VoltageLimitKind::ov,0,3}});
 CHECK(describe_voltage_limits_profile(native).targets==std::vector<std::string>({"voltage:delay:1","voltage:equation:2:0:3"}));const auto native_subset=select_voltage_limits_profile(native,{"voltage:delay:1"});CHECK(stage_voltage_limits_profile(native_subset,qualified.state()).size()==1&&sets==0);bad=native;bad["entries"].push_back(bad["entries"][0]);rejects([&]{describe_voltage_limits_profile(bad);});
}
static void catalog_contract(){
 fresh();{
  std::strcpy(device.uuid,"GPU-catalog-alternate");device.nvapi_handle=99;VoltageLimitsService service(api(),reinterpret_cast<dc_gpu*>(1),device);service.read(2);
  const auto a=request(service,VoltageLimitKind::switch_delay,1,30),b=request(service,VoltageLimitKind::switch_delay,12,40);const auto plan=service.preview({a,b});CHECK(plan.ok&&plan.state&&sets==0);CHECK(!service.preview({a,a}).ok&&sets==0&&!service.state().rows[0].stale);
  auto stale_draft=a;stale_draft.expected_value=19;CHECK(!service.preview({stale_draft}).ok&&!service.state().rows[0].stale&&service.state().rows[0].writable);CHECK(!service.verify(VoltageLimitsPlan{}).ok&&!service.state().rows[0].stale);
  auto bad=b;bad.key.index=7;CHECK(!service.preview({a,bad}).ok&&sets==0); // unrelated unsupported device never hides valid index1
  failed_gets=2;CHECK(!service.preview({a}).ok&&service.state().rows[0].stale&&service.state().rows[0].value==20&&sets==0);service.read(2);
  auto stale_plan=service.preview({a});di[8+56+3]=6;CHECK(!service.apply_profile(stale_plan,{}).ok&&sets==0);di[8+56+3]=5;service.read(2);
  VoltageLimitsReceipt receipt;int journal=0;CHECK(service.apply_profile(plan,[&](const auto& r){CHECK(sets==0);receipt=r;++journal;}).ok&&sets==1&&journal==1);CHECK(service.verify(plan).ok&&service.verify(receipt).ok&&!service.verify(receipt,true).ok&&!service.state().rows[0].stale);
  failed_gets=2;CHECK(!service.verify(plan).ok&&service.state().rows[0].stale&&service.state().rows[0].writable&&service.state().rows[0].value==30);CHECK(service.verify(receipt).ok&&!service.state().rows[0].stale);
  put(dc,8+12*12+4,45);CHECK(!service.verify(plan).ok&&service.verify(receipt).ok); // entire desired selection includes no-op index12
  CHECK(service.restore(receipt).ok&&get(dc,24)==20&&get(dc,8+12*12+4)==45&&service.verify(receipt,true).ok); // issued-only restore preserves foreign no-op field
  VoltageLimitsService other(api(),reinterpret_cast<dc_gpu*>(1),device);other.read(2);const auto before=sets;CHECK(!other.verify(plan).ok&&!other.restore(receipt).ok&&sets==before&&!other.state().rows[0].stale);
 }
 fresh();{
  VoltageLimitsService service(api(),reinterpret_cast<dc_gpu*>(1),device);service.read(2);CHECK(service.apply(request(service,VoltageLimitKind::switch_delay,1,25)).ok);const auto old_sets=sets;auto noop=service.preview({request(service,VoltageLimitKind::switch_delay,1,25)});int journal=0;CHECK(service.apply_profile(noop,[&](const auto&){++journal;}).ok&&sets==old_sets&&journal==0&&service.state().undo_available);
  const auto plan=service.preview({request(service,VoltageLimitKind::switch_delay,12,50)});VoltageLimitsReceipt receipt;CHECK(service.apply_profile(plan,[&](const auto& r){receipt=r;}).ok&&service.restore(receipt).ok&&get(dc,8+12*12+4)==40&&service.state().undo_available);CHECK(service.undo().ok&&get(dc,24)==20); // earlier standalone Undo reconstructed
 }
 fresh();{
  VoltageLimitsService service(api(),reinterpret_cast<dc_gpu*>(1),device);service.read(2);const auto plan=service.preview({request(service,VoltageLimitKind::switch_delay,1,30),request(service,VoltageLimitKind::switch_delay,12,50)});VoltageLimitsReceipt receipt;catalog_fail_at=2;int journal=0;const auto result=service.apply_profile(plan,[&](const auto& r){receipt=r;++journal;});CHECK(!result.ok&&result.may_have_changed&&sets==2&&journal==2&&service.state().recovery_pending);CHECK(service.verify(receipt).ok);catalog_fail_at=0;
  put(dc,24,99);const auto before=sets;CHECK(!service.restore(receipt).ok&&sets==before);put(dc,24,30);const auto opaque=dc[2];dc[2]^=0x11;CHECK(service.restore(receipt).ok&&get(dc,24)==20&&get(dc,8+12*12+4)==40&&dc[2]==uint8_t(opaque^0x11)&&!service.state().recovery_pending);
 }
 fresh();{
  VoltageLimitsService service(api(),reinterpret_cast<dc_gpu*>(1),device);service.read(2);CHECK(service.apply(request(service,VoltageLimitKind::switch_delay,1,25)).ok);auto plan=service.preview({request(service,VoltageLimitKind::switch_delay,12,50)});VoltageLimitsReceipt receipt;no_dispatch=true;const auto before=sets;CHECK(!service.apply_profile(plan,[&](const auto& r){receipt=r;}).ok&&sets==before&&service.state().undo_available&&!service.state().recovery_pending);CHECK(!service.verify(receipt).ok&&service.undo().ok&&get(dc,24)==20);
 }
 fresh();equations=true;{
  VoltageLimitsService service(api(true),reinterpret_cast<dc_gpu*>(1),device);service.read();const auto plan=service.preview({request(service,VoltageLimitKind::switch_delay,1,30),request(service,VoltageLimitKind::ov,3,1250000)});CHECK(plan.ok);VoltageLimitsReceipt receipt;CHECK(service.apply_profile(plan,[&](const auto& r){receipt=r;}).ok&&sets==2&&service.verify(plan).ok);CHECK(service.restore(receipt).ok&&get(dc,24)==20&&voltage(ec,0x408+3*0x80+4)==1200000);
 }
 fresh();{
  VoltageLimitsService service(api(),reinterpret_cast<dc_gpu*>(1),device);service.read(2);auto delay=request(service,VoltageLimitKind::switch_delay,1,30);VoltageLimitRequest unknown{{VoltageLimitKind::ov,0,3},1250000,{},"unbound",{}, {}};CHECK(!service.preview({delay,unknown}).ok&&sets==0&&service.state().rows[0].writable); // no semantic provider is invented
  const auto before=binding_calls;bind_error=DC_ERR_INVALID;CHECK(!service.preview({delay}).ok&&binding_calls==before+1&&sets==0&&service.state().rows[0].stale&&service.state().rows[0].availability==VoltagePolicyAvailability::invalid);
 }
}
int main(){
 catalog_contract();
 exchange_profiles();
 fresh();{VoltageLimitsService s(api(),reinterpret_cast<dc_gpu*>(1),device);auto data=s.read();CHECK(data.rows.size()==3&&data.rows[0].writable&&!data.rows[1].writable&&data.rows[2].writable);CHECK(data.equations_message.find("exports")!=std::string::npos);
 auto r=request(s,VoltageLimitKind::switch_delay,1,250);CHECK(s.apply(r).ok&&get(dc,24)==250&&get(dc,28)==0xC0FFEE);CHECK(s.undo().ok&&get(dc,24)==20);
 failed_gets=2;data=s.read(2);CHECK(data.rows[0].stale&&data.rows[0].writable&&data.rows[0].value==20);CHECK(!s.read(2).rows[0].stale);
 auto bad=request(s,VoltageLimitKind::switch_delay,12,0);const auto before=sets;CHECK(!s.apply(std::vector<VoltageLimitRequest>{r,bad}).ok&&sets==before);CHECK(!s.apply(std::vector<VoltageLimitRequest>{r,r}).ok&&sets==before);bad=r;bad.binding+="foreign";CHECK(!s.apply(bad).ok&&sets==before);
 auto doc=capture_voltage_limits_profile(s.state(),{{VoltageLimitKind::switch_delay,0,1}});doc["entries"][0]["value"]=350;auto staged=stage_voltage_limits_profile(doc,s.state());CHECK(staged.size()==1&&sets==before);CHECK(s.apply(staged).ok&&get(dc,24)==350);CHECK(s.restore_initial({VoltageLimitKind::switch_delay,0,1}).ok&&get(dc,24)==20);
 doc["entries"][0]["value"]=1.5;rejects([&]{stage_voltage_limits_profile(doc,s.state());});}
 fresh();{VoltageLimitsService s(api(),reinterpret_cast<dc_gpu*>(1),device);s.read(2);nt_after=true;auto r=s.apply(request(s,VoltageLimitKind::switch_delay,1,30));CHECK(!r.ok&&r.rollback_verified&&sets==2&&get(dc,24)==20);CHECK(resets==0);}
 fresh();{VoltageLimitsService s(api(),reinterpret_cast<dc_gpu*>(1),device);s.read(2);const auto opaque=dc[2];drift_after=true;auto r=s.apply(request(s,VoltageLimitKind::switch_delay,1,30));CHECK(!r.ok&&r.rollback_verified&&get(dc,24)==20&&dc[2]==uint8_t(opaque^0x17));}
 fresh();{VoltageLimitsService s(api(),reinterpret_cast<dc_gpu*>(1),device);s.read(2);block_after=4;auto r=s.apply(request(s,VoltageLimitKind::switch_delay,1,30));CHECK(!r.ok&&!r.rollback_verified&&sets==1&&s.state().recovery_pending&&s.state().rows[0].stale);failed_gets=0;put(dc,24,99);CHECK(!s.recover().ok&&sets==1);put(dc,24,30);CHECK(s.recover().ok&&get(dc,24)==20);}
 fresh();{VoltageLimitsService s(api(),reinterpret_cast<dc_gpu*>(1),device);s.read(2);foreign_after=true;auto r=s.apply(request(s,VoltageLimitKind::switch_delay,1,30));CHECK(!r.ok&&s.state().recovery_pending&&sets==1);foreign=false;CHECK(s.recover().ok&&get(dc,24)==20);}
 fresh();{VoltageLimitsService s(api(),reinterpret_cast<dc_gpu*>(1),device);s.read(2);auto pending=request(s,VoltageLimitKind::switch_delay,1,30);const auto binds=binding_calls;bind_error=DC_ERR_IDENTITY;CHECK(!s.apply(pending).ok&&sets==0&&binding_calls==binds+1);s.read(2);const auto captures=transport_calls;transport_error=DC_ERR_MALFORMED;CHECK(!s.apply(pending).ok&&sets==0&&transport_calls==captures+1);}
 fresh();{std::strcpy(device.uuid,"GPU-other-board");device.nvapi_handle=99;device.nvapi_devid=0x1234;VoltageLimitsService s(api(),reinterpret_cast<dc_gpu*>(1),device);s.read(2);no_dispatch=true;CHECK(!s.apply(request(s,VoltageLimitKind::switch_delay,12,UINT32_MAX)).ok&&sets==0&&!s.state().recovery_pending);CHECK(s.apply(request(s,VoltageLimitKind::switch_delay,12,UINT32_MAX)).ok&&get(dc,8+12*12+4)==UINT32_MAX);}
 fresh();{device.uuid[0]=0;VoltageLimitsService s(api(),reinterpret_cast<dc_gpu*>(1),device);CHECK(s.read(2).rows[0].writable);CHECK(s.apply(request(s,VoltageLimitKind::switch_delay,1,30)).ok&&sets==1);rejects([&]{capture_voltage_limits_profile(s.state(),{{VoltageLimitKind::switch_delay,0,1}});});auto pending=request(s,VoltageLimitKind::switch_delay,1,35);std::strcpy(device.uuid,"UUID-appeared-after-open");CHECK(!s.apply(pending).ok&&sets==1);}
 fresh();equations=true;{VoltageLimitsService s(api(),reinterpret_cast<dc_gpu*>(1),device);auto data=s.read();CHECK(data.equation_count==4&&data.definitions.size()==4&&data.rows.size()==3&&data.equations_message.find("role/graph")!=std::string::npos);CHECK(sets==0);}
 fresh();equations=true;{VoltageLimitsService s(api(true),reinterpret_cast<dc_gpu*>(1),device);auto data=s.read();CHECK(data.rows.size()==7&&data.rows[0].writable);const auto original_binding=data.rows[0].binding;bad_graph=true;data=s.read(1);CHECK(!data.rows[3].writable&&data.rows[4].writable&&data.rows[3].stale&&data.rows[3].value==1200000&&data.rows[3].initial_value==1200000&&data.rows[3].binding==original_binding);bad_graph=false;s.read(1);
 omit_ov=true;data=s.read(1);CHECK(data.rows.size()==7);auto missing=std::find_if(data.rows.begin(),data.rows.end(),[](const auto& r){return r.key.kind==VoltageLimitKind::ov;});CHECK(missing!=data.rows.end()&&missing->stale&&!missing->writable&&missing->value==1200000&&missing->initial_value==1200000&&missing->binding==original_binding);omit_ov=false;data=s.read(1);CHECK(!data.definitions_stale);
 failed_gets=2;data=s.read(1);CHECK(data.definitions_stale&&data.definitions.size()==4&&data.rows[0].key.kind==VoltageLimitKind::switch_delay&&!data.rows[0].stale);s.read(1);
 CHECK(s.apply(request(s,VoltageLimitKind::ov,3,1250000)).ok&&voltage(ec,0x408+3*0x80+4)==1250000);CHECK(s.undo().ok&&voltage(ec,0x408+3*0x80+4)==1200000);
 auto rel=request(s,VoltageLimitKind::rel,6,1150000);CHECK(s.apply(rel).ok); // upper-only never selects lower
 volts(ec,0x408+6*0x80+4,1000);CHECK(s.undo().ok&&voltage(ec,0x408+6*0x80+4)==1000&&voltage(ec,0x408+6*0x80+8)==1100000);
 auto vmin=request(s,VoltageLimitKind::vmin,5,1150000);const auto count=sets;CHECK(!s.apply(vmin).ok&&sets==count);
 auto fixed=request(s,VoltageLimitKind::rel,6,1120000);fixed.lower=1120000;CHECK(s.apply(fixed).ok);CHECK(s.restore_rom(fixed.key).ok&&voltage(ec,0x408+6*0x80+4)==0);}
 std::printf("Voltage limits service: %u checks\n",checks);return 0;
}
