// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Reuse byte fixtures only. Neither legacy test entry point nor its synthetic
// semantic evidence callback is called: every test below uses the real provider.
#include "voltage_evidence_provider.h"
#include "core_client.h"
#include "../../src/core/sha256.h"
#define main unused_vbios_graph_fixture_main
#include "vbios_voltage_graph_test.cpp"
#undef main
#define checks limits_fixture_checks
#define device limits_fixture_device
#define main unused_voltage_limits_fixture_main
#include "voltage_limits_service_test.cpp"
#undef main
#undef device
#undef checks
#undef CHECK
static unsigned provider_checks=0;
#define CHECK(x) do{++provider_checks;if(!(x)){std::fprintf(stderr,"Provider %d: %s\n",__LINE__,#x);std::abort();}}while(false)
namespace {
Bytes rom;unsigned image_reads=0,peer_reads=0,raw_reads=0,state_reads=0;int image_failures=0,image_error=DC_ERR_TRANSIENT,state_failures=0;dc_status state_failure=DC_E_FAILED;
bool peer_drift=false,peer_failed=false,rail_one=false,boost_missing=false;uint8_t peer_opaque=0xa7;
void current_target(dc_target& target){target={};target.size=sizeof(target);std::strcpy(target.slot,limits_fixture_device.slot);target.architecture=7;target.device_id=limits_fixture_device.nvapi_devid;target.subsystem_id=limits_fixture_device.nvapi_subsys;target.handle=limits_fixture_device.nvapi_handle;}
dc_status DC_CALL read_image(dc_gpu*,uint8_t* out,uint32_t capacity,uint32_t* size,dc_error* error){++image_reads;if(image_failures){--image_failures;error->kind=image_error;return DC_E_FAILED;}CHECK(capacity>=rom.size());std::memcpy(out,rom.data(),rom.size());*size=static_cast<uint32_t>(rom.size());return DC_OK;}
dc_status DC_CALL read_peers(dc_gpu*,dc_voltage_peers* out,dc_error* error){++peer_reads;if(peer_failed){error->kind=DC_ERR_TRANSIENT;return DC_E_FAILED;}const auto size=out->size;*out={};out->size=size;current_target(out->target);
 out->pstates.state=DC_VOLTAGE_PEER_AVAILABLE;out->pstates.length=DC_VOLTAGE_PEER_PSTATES_MAX;out->pstates_bytes[0]=0xf8;out->pstates_bytes[1]=0x1c;out->pstates_bytes[2]=2;out->pstates_bytes[101]=peer_opaque;
 out->clock_control.state=out->boost.state=out->curve.state=DC_VOLTAGE_PEER_ABSENT;out->voltage_boost.state=boost_missing?DC_VOLTAGE_PEER_UNSUPPORTED:DC_VOLTAGE_PEER_AVAILABLE;out->voltage_boost.status=boost_missing?-104:0;out->voltage_boost.length=boost_missing?0u:40u;out->voltage_boost_percent=0;return DC_OK;}
int32_t DC_CALL evidence_exports(const dc_gpu*,uint32_t){return 1;}
dc_status DC_CALL refresh_rails(dc_gpu*){return DC_OK;}
dc_status DC_CALL rail_caches(dc_gpu*,dc_rails_caches* out){const auto size=out->size;*out={};out->size=size;for(unsigned k=0;k<2;++k){out->has_known[k]=out->has_version[k]=1;out->known[k]=rail_one?3u:1u;out->version[k]=k?DC_RAILS_LIVE_VERSION:DC_RAILS_CONTROL_VERSION;
  for(unsigned r=0;r<2;++r){auto& d=out->diagnostics[k][r];d.present=1;d.version=out->version[k];d.status=r==1&&!rail_one?-9:0;d.valid=r==0||rail_one;d.attempts=1;}}return DC_OK;}
dc_status DC_CALL read_deltas(dc_gpu*,dc_rails_delta_set* out){out->present=rail_one?3u:1u;for(unsigned r=0;r<2;++r){out->rails[r]={};out->rails[r].type=1;}return DC_OK;}
dc_status DC_CALL read_state(dc_gpu*,dc_rails_state_set* out){out->present=rail_one?3u:1u;for(unsigned r=0;r<2;++r){auto& row=out->rails[r];row={};row.type=r==0?1u:3u;row.live=row.reliability=row.effective=900;row.alt_reliability=900+double(voltage(ec,0x408+1*0x80+4))/1000;row.overvoltage=double(voltage(ec,0x408+2*0x80+4))/1000;row.vmin=double(voltage(ec,0x408+4*0x80+4))/1000;}return DC_OK;}
dc_status DC_CALL read_evidence_state(dc_gpu* gpu,dc_rails_state_set* out){++state_reads;if(state_failures){--state_failures;return state_failure;}return read_state(gpu,out);}
dc_status DC_CALL read_blocks(dc_gpu* gpu,int32_t which,uint32_t version,dc_rails_block_set* out){++raw_reads;out->present=rail_one?3u:1u;std::memset(out->blocks,0,sizeof(out->blocks));dc_rails_state_set state{};state.size=sizeof(state);read_state(gpu,&state);
 for(unsigned r=0;r<2;++r){auto* block=out->blocks[r];const uint32_t mask=1u<<r;std::memcpy(block,&version,4);std::memcpy(block+4,&mask,4);const size_t at=0x48+r*0x54;const uint32_t type=1;std::memcpy(block+at,&type,4);
  if(which==DC_RAILS_ABS){const auto& row=state.rails[r];const double values[]={row.live,row.reliability,row.alt_reliability,row.overvoltage,row.effective,row.vmin};for(size_t k=0;k<6;++k){const auto value=static_cast<uint32_t>(std::llround(values[k]*1000));std::memcpy(block+at+4+4*k,&value,4);}}
  block[0x700]=0x5b;
 }return DC_OK;}
dc_status DC_CALL provider_escape(dc_gpu* gpu,uint8_t* bytes,size_t size,const dc_escape_fields* fields,int32_t* nt,dc_error* error){const auto command=get(Bytes(bytes,bytes+68),56);const auto result=escape(gpu,bytes,size,fields,nt,error);
 if(command==0x2080E0B7&&peer_drift){peer_opaque^=1;peer_drift=false;}return result;}
dc_status DC_CALL provider_hash(const uint8_t* bytes,size_t length,char out[65]){if(!bytes||!out)return DC_E_ARGUMENT;const auto hash=druta::sha256_hex(bytes,length);std::memcpy(out,hash.c_str(),65);return DC_OK;}
VoltageEvidenceApi evidence_api(){VoltageEvidenceApi a;a.target=api().transport;a.target.escape=provider_escape;a.image=read_image;a.peers=read_peers;a.sha256=provider_hash;a.blocks=read_blocks;a.state=read_evidence_state;a.deltas=read_deltas;a.caches=rail_caches;a.refresh=refresh_rails;a.interface_present=evidence_exports;return a;}
VoltageLimitsApi limits_api(){auto a=api();a.transport.escape=provider_escape;a.interface_present=evidence_exports;a.equation_evidence=make_voltage_equation_provider(evidence_api(),limits_fixture_device);return a;}
void setup(bool object=false){fresh();equations=true;limits_fixture_device.nvapi_devid=object?0x99aa:0x2345;rom=fixture(object,static_cast<uint16_t>(limits_fixture_device.nvapi_devid));image_reads=peer_reads=raw_reads=state_reads=0;state_failures=0;state_failure=DC_E_FAILED;image_failures=0;image_error=DC_ERR_TRANSIENT;peer_drift=peer_failed=rail_one=boost_missing=false;peer_opaque=0xa7;
 const auto graph=parse_vbios_voltage_graph(rom);CHECK(graph.ok&&graph.equations.size()==6);ei.assign(0xE0408,0);ec.assign(0x100408,0);ei[0]=ec[0]=3;
 for(const auto& equation:graph.equations){const auto n=equation.index;ei[4+n/8]|=uint8_t(1u<<(n%8));ec[4+n/8]|=uint8_t(1u<<(n%8));const size_t at=0x408+n*0x70,ct=0x408+n*0x80;const bool rel=equation.kind==7;const uint8_t type=rel?6:3;ei[at]=ei[at+0x6c]=ec[ct]=ec[ct+0x7c]=type;word(ei,at+2,equation.next);ei[at+0x6d]=equation.variable;ei[at+0x6e]=equation.output;
  const int64_t value=rel?1130000:n==2?1210000:n==4?760000:int64_t(equation.upper);const float lower=rel?900000.0f:static_cast<float>(value);number(ec,ct+4,lower);number(ec,ct+8,static_cast<float>(value));
  if(rel){ec[ct+0x78]=static_cast<uint8_t>(equation.variables.size());ec[ct+0x79]=static_cast<uint8_t>(equation.terms.size());
   for(const auto pair:{std::pair<size_t,const std::vector<VbiosVoltageTerm>*>{0x10,&equation.variables},{0x28,&equation.terms}})for(size_t k=0;k<pair.second->size();++k){const auto& term=pair.second->at(k);ec[ct+pair.first+8*k]=term.kind;std::copy_n(term.raw.begin()+1,4,ec.begin()+static_cast<ptrdiff_t>(ct+pair.first+8*k+4));}}
  else number(ec,ct+0x10,static_cast<float>(value));ec[ct+0x60]=0x9d;
 }
}
VoltageEquationPackets packets(){VoltageEquationPackets p;current_target(p.target);p.info=ei;p.control=ec;return p;}
const VoltageLimitRow& find_row(const VoltageLimitsData& data,VoltageLimitKind kind){for(const auto& r:data.rows)if(r.key.kind==kind&&r.key.rail==0)return r;throw std::runtime_error("Missing provider role");}
void ready(const VoltageLimitsData& data){for(const auto& row:data.rows)if(row.key.kind!=VoltageLimitKind::switch_delay&&!row.writable)std::fprintf(stderr,"Role %u: %s\n",unsigned(row.key.kind),row.message.c_str());}
uint32_t DC_CALL provider_version(){return DC_ABI_VERSION;}
dc_status DC_CALL provider_enumerate(dc_gpu_entry* entries,size_t capacity,size_t* count){*count=1;if(!entries||!capacity)return DC_E_BUFFER;entries[0]={};entries[0].size=sizeof(entries[0]);std::strcpy(entries[0].slot,limits_fixture_device.slot);std::strcpy(entries[0].uuid,limits_fixture_device.uuid);entries[0].has_nvapi=entries[0].has_devid=1;entries[0].devid=limits_fixture_device.nvapi_devid;entries[0].subsys=limits_fixture_device.nvapi_subsys;return DC_OK;}
dc_status DC_CALL provider_open(const char* slot,dc_gpu** out){if(std::strcmp(slot,limits_fixture_device.slot))return DC_E_ARGUMENT;*out=reinterpret_cast<dc_gpu*>(1);return DC_OK;}
void DC_CALL provider_close(dc_gpu*){}
void client_wiring(){setup(true);CoreApi core;core.abi_version=provider_version;core.enumerate=provider_enumerate;core.open=provider_open;core.close=provider_close;core.identity=id;core.lock=lock;core.unlock=unlock;
 core.voltage_limits=api();core.voltage_limits.transport.escape=provider_escape;core.voltage_limits.interface_present=evidence_exports;core.voltage_evidence=evidence_api();
 auto client=create_core_backend_for_test(core);std::string error;CHECK(client->open(limits_fixture_device.slot,error)&&image_reads==0&&peer_reads==0&&sets==0);
 Command read;read.operation=Command::Operation::voltage_limits_read;read.voltage_families=1;const auto result=client->execute(read);if(!result.ok)std::fprintf(stderr,"Client provider read: %s\n",result.message.c_str());CHECK(result.ok&&image_reads>=2&&sets==0);
 const auto before=client->read(false).voltage_limits;CHECK(before&&find_row(*before,VoltageLimitKind::ov).writable);const auto& row=find_row(*before,VoltageLimitKind::ov);
 Command apply;apply.operation=Command::Operation::voltage_limits_apply;apply.voltage_limit_requests=std::make_shared<const std::vector<VoltageLimitRequest>>(std::vector<VoltageLimitRequest>{{row.key,1250000,{},row.binding,row.value,row.lower}});CHECK(client->execute(apply).ok&&sets==1&&find_row(*before,VoltageLimitKind::ov).value==1210000);
 Command undo;undo.operation=Command::Operation::voltage_limits_undo;CHECK(client->execute(undo).ok&&sets==2&&voltage(ec,0x408+2*0x80+4)==1210000);client->close();
}
}
int main(){
 for(bool object:{false,true}){setup(object);auto a=limits_api();CHECK(image_reads==0&&peer_reads==0&&sets==0);VoltageLimitsService service(a,reinterpret_cast<dc_gpu*>(1),limits_fixture_device);const auto& data=service.read(1);ready(data);
  const auto& ov=find_row(data,VoltageLimitKind::ov);CHECK(ov.writable&&ov.value==1210000&&ov.stored_value==1200000);CHECK(find_row(data,VoltageLimitKind::rel).stored_value==1120000&&find_row(data,VoltageLimitKind::rel).value==1130000);CHECK(!data.rows.empty()&&image_reads>=2&&peer_reads>0&&sets==0);
  const auto original=ec;CHECK(service.apply(request(service,VoltageLimitKind::ov,2,1250000)).ok&&sets==1);CHECK(ec[0x408+2*0x80+0x60]==0x9d&&find_row(service.state(),VoltageLimitKind::ov).value==1250000);CHECK(service.undo().ok&&ec==original&&sets==2);
 }
 for(bool object:{false,true})for(const auto selected:{VoltageLimitKind::rel,VoltageLimitKind::alt,VoltageLimitKind::vmin}){
  setup(object);VoltageLimitsService service(limits_api(),reinterpret_cast<dc_gpu*>(1),limits_fixture_device);service.read(1);const auto original=ec;const auto& before=find_row(service.state(),selected);CHECK(before.writable);const auto index=before.key.index;const int64_t wanted=selected==VoltageLimitKind::rel?1150000:selected==VoltageLimitKind::alt?30000:780000;
  const auto applied=service.apply(request(service,selected,index,wanted));if(!applied.ok)std::fprintf(stderr,"Provider role Apply %u: %s\n",unsigned(selected),applied.message.c_str());CHECK(applied.ok&&sets==1&&find_row(service.state(),selected).value==wanted);CHECK(ec[0x408+index*0x80+0x60]==0x9d);
  const auto& after=find_row(service.state(),selected);if(selected==VoltageLimitKind::rel)CHECK(after.lower==900000&&after.reported==900000);else CHECK(after.reported==(selected==VoltageLimitKind::alt?930000:780000));
  const auto undone=service.undo();if(!undone.ok)std::fprintf(stderr,"Provider role Undo %u: %s\n",unsigned(selected),undone.message.c_str());CHECK(undone.ok&&sets==2&&ec==original);
 }
 setup(true);{VoltageLimitsService service(limits_api(),reinterpret_cast<dc_gpu*>(1),limits_fixture_device);service.read(1);const auto original=ec;std::vector<VoltageLimitKey> selection;for(const auto kind:{VoltageLimitKind::rel,VoltageLimitKind::alt,VoltageLimitKind::ov,VoltageLimitKind::vmin})selection.push_back(find_row(service.state(),kind).key);
  auto document=capture_voltage_limits_profile(service.state(),selection);CHECK(document["entries"].size()==4&&sets==0);for(auto& entry:document["entries"]){const auto kind=static_cast<VoltageLimitKind>(entry["kind"].get<int>());entry["value"]=kind==VoltageLimitKind::rel?1150000:kind==VoltageLimitKind::alt?30000:kind==VoltageLimitKind::ov?1250000:780000;}
  const auto staged=stage_voltage_limits_profile(document,service.state());CHECK(staged.size()==4&&sets==0);for(const auto& row:staged)CHECK(row.expected_value==find_row(service.state(),row.key.kind).value&&!row.binding.empty());
  const auto applied=service.apply(staged);if(!applied.ok)std::fprintf(stderr,"Provider manual profile Apply: %s\n",applied.message.c_str());CHECK(applied.ok&&sets==4);for(const auto& row:staged)CHECK(find_row(service.state(),row.key.kind).value==row.value);CHECK(service.undo().ok&&sets==8&&ec==original);
 }
 setup();{auto provider=make_voltage_equation_provider(evidence_api(),limits_fixture_device);const auto evidence=provider(reinterpret_cast<dc_gpu*>(1),packets());CHECK(evidence.retained_requests_qualified&&evidence.rails[0].present&&!evidence.rails[1].present&&sets==0);const auto before=evidence.clock_requests;peer_opaque^=1;const auto changed=provider(reinterpret_cast<dc_gpu*>(1),packets());CHECK(changed.clock_requests!=before&&sets==0);}
 setup();{boost_missing=true;VoltageLimitsService service(limits_api(),reinterpret_cast<dc_gpu*>(1),limits_fixture_device);service.read(1);CHECK(find_row(service.state(),VoltageLimitKind::ov).writable&&find_row(service.state(),VoltageLimitKind::vmin).writable&&!find_row(service.state(),VoltageLimitKind::rel).writable&&sets==0);}
 setup();{state_failures=1;state_failure=DC_E_ARGUMENT;VoltageLimitsService service(limits_api(),reinterpret_cast<dc_gpu*>(1),limits_fixture_device);const auto& data=service.read(1);
  CHECK(state_reads==1&&data.definitions.size()==6&&data.equation_count==6&&sets==0);CHECK(data.rows.empty()&&data.equations_message.find("Independent rail policy/CONTROL capture failed")!=std::string::npos);
 }
 setup();{state_failures=1;state_failure=DC_E_FAILED;VoltageLimitsService service(limits_api(),reinterpret_cast<dc_gpu*>(1),limits_fixture_device);const auto& data=service.read(1);
  CHECK(state_reads>=3&&state_reads<=5&&data.definitions.size()==6&&sets==0);const auto& row=find_row(data,VoltageLimitKind::ov);CHECK(row.writable&&!row.stale&&row.value==1210000&&row.availability==VoltagePolicyAvailability::available);
 }
 setup();{image_failures=1;auto provider=make_voltage_equation_provider(evidence_api(),limits_fixture_device);const auto evidence=provider(reinterpret_cast<dc_gpu*>(1),packets());CHECK(evidence.retained_requests_qualified&&image_reads>=3&&image_reads<=8&&sets==0);}
 setup();{image_failures=5;image_error=DC_ERR_MALFORMED;auto provider=make_voltage_equation_provider(evidence_api(),limits_fixture_device);bool rejected=false;try{provider(reinterpret_cast<dc_gpu*>(1),packets());}catch(const VoltageEquationEvidenceError& e){rejected=e.kind==VoltagePolicyAvailability::invalid;}CHECK(rejected&&image_reads==1&&sets==0);}
 setup();{image_failures=5;image_error=DC_ERR_MALFORMED;VoltageLimitsService service(limits_api(),reinterpret_cast<dc_gpu*>(1),limits_fixture_device);const auto& data=service.read(1);
  CHECK(data.definitions.size()==6&&data.equation_count==6&&image_reads==1&&sets==0);CHECK(std::none_of(data.rows.begin(),data.rows.end(),[](const auto& row){return row.writable;}));
 }
 setup();{VoltageLimitsService service(limits_api(),reinterpret_cast<dc_gpu*>(1),limits_fixture_device);service.read(1);CHECK(find_row(service.state(),VoltageLimitKind::ov).writable);image_failures=5;image_error=DC_ERR_MALFORMED;const auto before=image_reads;service.read(1);const auto& row=find_row(service.state(),VoltageLimitKind::ov);
  CHECK(row.stale&&!row.writable&&row.availability==VoltagePolicyAvailability::invalid&&row.value==1210000&&service.state().definitions.size()==6&&image_reads==before+1&&sets==0);
 }
 setup();{auto a=limits_api();VoltageLimitsService service(a,reinterpret_cast<dc_gpu*>(1),limits_fixture_device);service.read(1);CHECK(find_row(service.state(),VoltageLimitKind::ov).writable);ec[0x408+0x10+4]^=1;service.read(1);CHECK(!find_row(service.state(),VoltageLimitKind::rel).writable&&sets==0);}
 setup();{VoltageLimitsService service(limits_api(),reinterpret_cast<dc_gpu*>(1),limits_fixture_device);service.read(1);CHECK(find_row(service.state(),VoltageLimitKind::ov).writable);
  word(ei,0x408+5*0x70+2,2);service.read(1);CHECK(!find_row(service.state(),VoltageLimitKind::ov).writable&&sets==0); // A non-root loaded dependency must not create sharing absent from ROM.
 }
 setup();{VoltageLimitsService service(limits_api(),reinterpret_cast<dc_gpu*>(1),limits_fixture_device);service.read(1);CHECK(find_row(service.state(),VoltageLimitKind::ov).writable);
  const size_t info=0x408+6*0x70,control=0x408+6*0x80;std::copy_n(ei.begin()+0x408+3*0x70,0x70,ei.begin()+static_cast<ptrdiff_t>(info));std::copy_n(ec.begin()+0x408+3*0x80,0x80,ec.begin()+static_cast<ptrdiff_t>(control));word(ei,info+2,2);ei[4]=uint8_t(ei[4]|(1u<<6));ec[4]=uint8_t(ec[4]|(1u<<6));
  service.read(1);CHECK(service.state().definitions.size()==7&&!find_row(service.state(),VoltageLimitKind::ov).writable&&sets==0); // An unbound additional active consumer cannot be ignored.
 }
 setup();{auto provider=make_voltage_equation_provider(evidence_api(),limits_fixture_device);foreign=true;const auto before=image_reads;bool rejected=false;try{provider(reinterpret_cast<dc_gpu*>(1),packets());}catch(const VoltageEquationEvidenceError& e){rejected=e.kind==VoltagePolicyAvailability::invalid;}CHECK(rejected&&image_reads==before&&sets==0);}
 setup();{VoltageLimitsService service(limits_api(),reinterpret_cast<dc_gpu*>(1),limits_fixture_device);service.read(1);CHECK(find_row(service.state(),VoltageLimitKind::ov).writable);peer_failed=true;service.read(1);CHECK(find_row(service.state(),VoltageLimitKind::ov).stale&&find_row(service.state(),VoltageLimitKind::ov).value==1210000&&sets==0);peer_failed=false;service.read(1);CHECK(!find_row(service.state(),VoltageLimitKind::ov).stale&&find_row(service.state(),VoltageLimitKind::ov).writable&&sets==0);}
 setup();{auto a=limits_api();VoltageLimitsService service(a,reinterpret_cast<dc_gpu*>(1),limits_fixture_device);service.read(1);peer_drift=true;const auto result=service.apply(request(service,VoltageLimitKind::ov,2,1250000));CHECK(!result.ok&&result.may_have_changed&&(result.rollback_verified||service.state().recovery_pending)&&peer_opaque==uint8_t(0xa7^1));CHECK(sets>=1);if(result.rollback_verified)CHECK(voltage(ec,0x408+2*0x80+4)==1210000);}
 client_wiring();std::printf("Production voltage evidence provider: %u checks\n",provider_checks);return 0;
}
