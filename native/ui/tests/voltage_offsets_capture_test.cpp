// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_scaffold_provider.h"
#include <windows.h>
#define main unused_provider_graph_fixture_main
#include "vbios_voltage_graph_test.cpp"
#undef main
namespace {
struct ProviderCase {std::vector<uint8_t> original,current,variables;VoltageEquationPackets before,loaded;VoltageScaffoldReceipt receipt;dc_gpu_identity identity{};};
ProviderCase test_case(){
 ProviderCase c;c.original=fixture(false,0x4567);const auto base=legacy_vfe+relative_base;const std::vector<uint8_t> terms(c.original.begin()+base+150,c.original.begin()+base+230);
 const std::array<uint8_t,25> empty={0,255,255,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,255};
 for(unsigned i=0;i<2;++i)std::copy(empty.begin(),empty.end(),c.original.begin()+base+25*(6+i));std::copy(terms.begin(),terms.end(),c.original.begin()+base+200);c.original[legacy_vfe+5]=8;
 for(unsigned i=1;i<6;++i)c.original[base+25*i+1]=7;
 auto& target=c.before.target;target.size=sizeof(target);std::strcpy(target.slot,"0000:37:00.0");target.handle=42;target.architecture=7;target.device_id=0x4567;target.subsystem_id=0xaabbccdd;
 c.before.info.resize(0xe0408);c.before.control.resize(0x100408);c.before.info[0]=c.before.control[0]=1;const auto graph=parse_vbios_voltage_graph(c.original);
 for(uint32_t i=0;i<6;++i){const auto& row=graph.equations[i];const auto at=0x408+0x70*i,ct=0x408+0x80*i;c.before.info[4]|=uint8_t(1u<<i);c.before.control[4]|=uint8_t(1u<<i);
  c.before.info[at]=c.before.info[at+0x6c]=c.before.control[ct]=c.before.control[ct+0x7c]=uint8_t(i?3:6);word(c.before.info,at+2,row.next);c.before.info[at+0x6d]=row.variable;c.before.info[at+0x6e]=2;
  number(c.before.control,ct+4,row.lower);number(c.before.control,ct+8,row.upper);if(i)number(c.before.control,ct+16,row.coefficients[0]);c.before.info[at+30]=0xa3;c.before.control[ct+0x60]=0x9d;
 }
 c.variables.resize(0xc364);c.variables[4]=128;c.receipt.source_sha256=std::string(64,'a');c.receipt.slot=target.slot;c.receipt.windows_target={target.slot,"PCI\\VEN_10DE&DEV_4567\\A","0007"};c.receipt.original_image=c.original;c.receipt.before=c.before;c.receipt.requests={{0,VbiosVoltageRole::ov}};
 const auto plan=plan_installed_voltage_scaffold(c.original,c.before,c.variables,c.receipt.requests);if(!plan.ok)throw std::runtime_error(plan.message);c.receipt.recorded_heads=plan.heads;c.receipt.historical_values["voltage.0.OV.offset"]=123000;c.current=*plan.planned_image;c.loaded=c.before;
 for(const auto& head:plan.heads)for(const bool info:{true,false}){auto& b=info?c.loaded.info:c.loaded.control;const uint32_t stride=info?0x70u:0x80u,at=0x408+head.index*stride;
  std::copy_n(b.begin()+0x408+stride,stride,b.begin()+at);b[4+head.index/8]|=uint8_t(1u<<(head.index%8));if(info)word(b,at+2,head.next);else for(const auto offset:{4u,8u,16u})number(b,at+offset,-5000);}
 c.identity.size=sizeof(c.identity);std::strcpy(c.identity.slot,target.slot);std::strcpy(c.identity.uuid,"GPU-scaffold-other");c.identity.nvapi_ok=c.identity.nvapi_selected=1;c.identity.nvapi_handle=42;c.identity.nvapi_devid=target.device_id;c.identity.nvapi_subsys=target.subsystem_id;return c;
}
ProviderCase* current_case=nullptr;int mode=0,images=0,variables=0,receipts=0,windows_reads=0,equations=0,locks=0,identities=0;
dc_status DC_CALL provider_lock(dc_gpu*){++locks;return DC_OK;}dc_status DC_CALL provider_unlock(dc_gpu*){--locks;return DC_OK;}
dc_status DC_CALL provider_identity(const dc_gpu*,dc_gpu_identity* out){++identities;*out=current_case->identity;return DC_OK;}
dc_status DC_CALL provider_bind(dc_gpu*,int32_t,dc_target* out,dc_error*){*out=current_case->before.target;return DC_OK;}
int32_t DC_CALL provider_arch(uint32_t architecture){return architecture==7;}
dc_status DC_CALL provider_transport(dc_gpu*,dc_rm_transport* out,dc_error*){out->captured=1;out->header_count=17;out->header[0]=0x4e564441;out->header[1]=0x10002;out->header[2]=76;out->header[3]=0x4e562a2a;out->header[4]=0x0500002b;out->header[15]=8;return DC_OK;}
dc_status DC_CALL provider_escape(dc_gpu*,uint8_t* raw,size_t size,const dc_escape_fields*,int32_t* nt,dc_error*){++variables;auto* p=reinterpret_cast<uint32_t*>(raw);check(p[14]==0x2080a0b1&&size==68+0xc364,"Provider only dispatches read-only variable INFO");p[11]=1;*nt=0;std::copy(current_case->variables.begin(),current_case->variables.end(),raw+68);if(mode==10)++p[14];return DC_OK;}
dc_status DC_CALL provider_image(dc_gpu*,uint8_t* out,uint32_t capacity,uint32_t* size,dc_error* error){++images;if(mode==6&&images==1){error->kind=DC_ERR_TRANSIENT;return DC_E_FAILED;}if(mode==7){error->kind=DC_ERR_MALFORMED;return DC_E_FAILED;}check(capacity>=current_case->current.size(),"Bounded ROM capacity");std::copy(current_case->current.begin(),current_case->current.end(),out);*size=static_cast<uint32_t>(current_case->current.size());return DC_OK;}
dc_status DC_CALL provider_hash(const uint8_t* bytes,size_t size,char out[65]){ // deterministic injected hash; no DLL or hardware
 uint64_t hash=1469598103934665603ull;for(size_t i=0;i<size;++i){hash^=bytes[i];hash*=1099511628211ull;}for(unsigned i=0;i<4;++i)std::snprintf(out+16*i,17,"%016llx",static_cast<unsigned long long>(hash+i));return DC_OK;}
VoltageScaffoldHostApi host(){VoltageScaffoldHostApi h;h.state_directory=[](){return std::wstring(L"C:\\private-state");};
 h.resolve_windows=[](const dc_gpu_identity&,const dc_target&){++windows_reads;auto target=current_case->receipt.windows_target;if(mode==8&&windows_reads>1)target.driver_key="0008";return target;};
 h.load_receipt=[](const std::wstring&,const dc_target&,const VoltageScaffoldWindowsTarget&){++receipts;if(mode==1)throw VoltageEquationEvidenceError("missing receipt",VoltagePolicyAvailability::unsupported);auto saved=current_case->receipt;if(mode==2)++saved.recorded_heads[0].next;if(mode==3&&receipts>1)++saved.historical_values.begin()->second;if(mode==11&&receipts>1)saved.source_sha256=std::string(64,'b');return saved;};
 h.capture_equations=[](dc_gpu*){++equations;if(mode==9)throw VoltageEquationEvidenceError("equation read failed",VoltagePolicyAvailability::transient);auto loaded=current_case->loaded;if(mode==4)++current_case->identity.nvapi_handle;if(mode==5)loaded.control[0x408+2*0x80+0x60]^=1;return loaded;};return h;}
void provider_tests(){auto c=test_case();current_case=&c;VoltageEvidenceApi api;api.target.identity=provider_identity;api.target.bind=provider_bind;api.target.architecture_supported=provider_arch;api.target.transport=provider_transport;api.target.escape=provider_escape;api.target.lock=provider_lock;api.target.unlock=provider_unlock;api.image=provider_image;api.sha256=provider_hash;
 const auto reset=[](){mode=images=variables=receipts=windows_reads=equations=locks=identities=0;};reset();auto provider=make_voltage_scaffold_provider(api,c.identity,host());check(images==0&&receipts==0&&identities==0,"Constructing scaffold provider performs zero I/O");
 const auto read=provider(reinterpret_cast<dc_gpu*>(1));check(read.heads.size()==1&&read.heads[0].value_uv==-5000&&read.heads[0].value_uv!=c.receipt.historical_values.begin()->second,"Actual signed request inspected; historical receipt value never applied");
 check(read.target.handle==42&&read.receipt_binding.size()==64&&read.image_sha256.size()==64&&images==2&&variables==2&&equations==1&&receipts==2&&windows_reads==2&&locks==0,"True factory brackets fresh ROM/variables/equations and rereads receipt/Windows");
 reset();mode=6;const auto recovered=provider(reinterpret_cast<dc_gpu*>(1));check(recovered.heads[0].value_uv==-5000&&images==3&&locks==0,"Only lower matching-pair helper retries transient ROM failure");
 for(int test:{1,2,3,4,5,7,8,9,10,11}){reset();mode=test;bool rejected=false;try{provider(reinterpret_cast<dc_gpu*>(1));}catch(const VoltageEquationEvidenceError& error){rejected=true;check(error.kind==(test==1?VoltagePolicyAvailability::unsupported:test==9?VoltagePolicyAvailability::transient:VoltagePolicyAvailability::invalid),"Typed scaffold failure classification retained");}check(rejected&&locks==0,"Forged/stale/missing evidence refused without retained lock");if(test==1)check(images==0&&variables==0&&equations==0,"Absent receipt does not start semantic hardware reads");if(test==7)check(images==1,"Malformed ROM read is not retried");if(test==9)check(equations==1,"Provider adds no outer retry");if(test==10)check(variables==1,"Malformed variable reply not retried");c.identity.nvapi_handle=42;}
 reset();c.identity.uuid[0]=0;auto no_uuid=make_voltage_scaffold_provider(api,c.identity,host());check(no_uuid(reinterpret_cast<dc_gpu*>(1)).heads[0].value_uv==-5000,"Matching UUID absence permits paired runtime inspection");
}
}
#include "voltage_offsets_capture.h"
namespace {
dc_status DC_CALL peers(dc_gpu*,dc_voltage_peers* p,dc_error*){const auto size=p->size;*p={};p->size=size;p->target=current_case->before.target;p->pstates.state=DC_VOLTAGE_PEER_AVAILABLE;p->pstates.length=4;p->pstates_bytes[3]=0x7a;p->voltage_boost.state=DC_VOLTAGE_PEER_AVAILABLE;p->voltage_boost.length=40;p->voltage_boost_percent=5;return DC_OK;}
dc_status DC_CALL state(dc_gpu*,dc_rails_state_set* p){p->present=0;return DC_OK;}
dc_status DC_CALL deltas(dc_gpu*,dc_rails_delta_set* p){p->present=0;return DC_OK;}
dc_status DC_CALL blocks(dc_gpu*,int32_t,uint32_t,dc_rails_block_set* p){p->present=0;return DC_OK;}
dc_status DC_CALL caches(dc_gpu*,dc_rails_caches* p){for(auto& family:p->diagnostics)for(auto& item:family){item.present=1;item.status=-104;}return DC_OK;}
int32_t DC_CALL present_export(const dc_gpu*,uint32_t){return 1;}
}
int main(){auto c=test_case();current_case=&c;VoltageEvidenceApi a;a.target.identity=provider_identity;a.target.bind=provider_bind;a.target.architecture_supported=provider_arch;a.target.transport=provider_transport;a.target.escape=provider_escape;a.target.lock=provider_lock;a.target.unlock=provider_unlock;a.image=provider_image;a.sha256=provider_hash;a.peers=peers;a.state=state;a.deltas=deltas;a.blocks=blocks;a.caches=caches;a.interface_present=present_export;
 auto capture=make_voltage_offsets_capture(a,c.identity,host());check(images==0&&variables==0&&receipts==0,"Factory has no I/O");const auto result=capture(reinterpret_cast<dc_gpu*>(1));check(result.scaffold.heads.size()==1&&result.scaffold.heads[0].value_uv==-5000&&result.equations.info==c.loaded.info&&result.equations.control==c.loaded.control,"Exact recognized equations retained");check(result.retained.retained_requests_qualified&&result.retained.image_sha256==result.scaffold.image_sha256&&!result.retained.image_sha256.empty()&&result.retained.clock_requests[7]==0x7a&&result.retained.boost_percent==5&&!result.retained.rails[0].present&&locks==0,"Independent absent rails and unknown peer bytes retained");
 mode=4;bool refused=false;try{capture(reinterpret_cast<dc_gpu*>(1));}catch(const VoltageEquationEvidenceError&){refused=true;}check(refused&&locks==0,"Changed target refused and recursive locks released");std::printf("Voltage offsets capture: %d checks, %d failures\n",checks,failures);return failures?1:0;}
