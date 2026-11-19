// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_variable_capture.h"
#include <algorithm>
#include <array>
#include <cstring>
namespace druta::ui {
namespace {
using Availability=VoltagePolicyAvailability;
[[noreturn]] void fail(const char* s,Availability a=Availability::invalid){throw VoltageEquationEvidenceError(s,a);}
Availability classify(const dc_error& e){return e.kind==DC_ERR_TRANSIENT||e.kind==DC_ERR_UNSTABLE?Availability::transient:e.kind==DC_ERR_UNSUPPORTED?Availability::unsupported:Availability::invalid;}
bool terminated(const char* p,size_t n){return std::memchr(p,0,n)!=nullptr;}
bool same(const dc_target& a,const dc_target& b){return a.size==sizeof(a)&&b.size==sizeof(b)&&a.architecture==b.architecture&&a.device_id==b.device_id&&a.subsystem_id==b.subsystem_id&&a.handle==b.handle&&std::strcmp(a.slot,b.slot)==0;}
struct Reader {
 const VoltagePolicyApi& api; dc_gpu* gpu; const dc_gpu_identity& opened; dc_target first{}; bool have=false;
 dc_target bind(){dc_gpu_identity id{};id.size=sizeof(id);const auto identity_status=api.identity(gpu,&id);if(identity_status!=DC_OK)fail("Variable target identity unavailable",identity_status==DC_E_ARGUMENT?Availability::invalid:identity_status==DC_E_UNAVAILABLE?Availability::unsupported:Availability::transient);
  if(opened.size!=sizeof(opened)||id.size!=sizeof(id)||!terminated(opened.slot,sizeof(opened.slot))||!terminated(opened.uuid,sizeof(opened.uuid))||!terminated(id.slot,sizeof(id.slot))||!terminated(id.uuid,sizeof(id.uuid))||!terminated(opened.pairing_error,sizeof(opened.pairing_error))||!terminated(id.pairing_error,sizeof(id.pairing_error))||opened.pairing_error[0]||!id.nvapi_ok||!id.nvapi_selected||id.pairing_error[0]||std::strcmp(opened.slot,id.slot)||std::strcmp(opened.uuid,id.uuid)||opened.nvapi_handle!=id.nvapi_handle||opened.nvapi_devid!=id.nvapi_devid||opened.nvapi_subsys!=id.nvapi_subsys||opened.nvml_selected!=id.nvml_selected||opened.nvml_has_devid!=id.nvml_has_devid||opened.nvml_devid!=id.nvml_devid||opened.nvml_subsys!=id.nvml_subsys)fail("Variable target identity changed");
  dc_target t{};t.size=sizeof(t);dc_error e{};e.size=sizeof(e);if(api.bind(gpu,1,&t,&e)!=DC_OK)fail("Variable target binding unavailable",classify(e));
  if(t.size!=sizeof(t)||!terminated(t.slot,sizeof(t.slot))||!t.slot[0]||!t.handle||std::strcmp(t.slot,opened.slot)||t.handle!=opened.nvapi_handle||t.device_id!=opened.nvapi_devid||t.subsystem_id!=opened.nvapi_subsys)fail("Variable target binding mismatch");
  if(!api.architecture_supported(t.architecture))fail("Variable layout unsupported",Availability::unsupported);if(have&&!same(first,t))fail("Variable target changed");first=t;have=true;return t;}
 std::vector<uint8_t> read(){bind();dc_rm_transport t{};t.size=sizeof(t);dc_error e{};e.size=sizeof(e);if(api.transport(gpu,&t,&e)!=DC_OK)fail("Variable transport unavailable",classify(e));
  if(t.size!=sizeof(t)||t.captured!=1||t.header_count!=17||uint64_t(t.header[2])!=68+uint64_t(t.header[15])||t.header[0]!=0x4e564441||t.header[1]!=0x10002||t.header[3]!=0x4e562a2a||t.header[4]!=0x0500002b)fail("Variable transport envelope malformed");
  constexpr size_t length=0xc364,total=68+length;std::vector<uint32_t> words(total/4+32,0xa5a5a5a5);auto* p=words.data()+16;std::fill(p,p+total/4,0);std::copy(t.header,t.header+17,p);p[2]=total;p[14]=0x2080a0b1;p[15]=length;p[16]=p[5]=p[11]=0;std::array<uint32_t,16> expected{};std::copy(p,p+16,expected.begin());bind();int32_t nt=0;e={};e.size=sizeof(e);const auto status=api.escape(gpu,reinterpret_cast<uint8_t*>(p),total,&t.fields,&nt,&e);
  if(!std::all_of(words.begin(),words.begin()+16,[](uint32_t v){return v==0xa5a5a5a5;})||!std::all_of(words.end()-16,words.end(),[](uint32_t v){return v==0xa5a5a5a5;}))fail("Variable packet guard changed");bind();for(size_t i=0;i<16;++i)if(i!=5&&i!=11&&p[i]!=expected[i])fail("Variable response envelope changed");if(p[5]>1||p[11]>1)fail("Variable response context or completion malformed");if(status!=DC_OK)fail("Variable INFO escape failed",status==DC_E_UNAVAILABLE?Availability::unsupported:classify(e));if(nt)fail("Variable INFO NTSTATUS failed",Availability::transient);
  if(p[11]!=1)fail("Variable INFO incomplete",Availability::transient);if(p[16])fail("Variable INFO RM rejected",p[16]==0x1f||p[16]==0x56?Availability::unsupported:Availability::transient);
  const auto* b=reinterpret_cast<const uint8_t*>(p+17);return {b,b+length};}
};
}
VoltageVariableCapture capture_voltage_variables(const VoltagePolicyApi& api,dc_gpu* gpu,const dc_gpu_identity& opened){if(!gpu||!api.identity||!api.bind||!api.architecture_supported||!api.transport||!api.escape)fail("Variable INFO interfaces unavailable",Availability::unsupported);
 for(int attempt=0;;++attempt){try{Reader r{api,gpu,opened};auto first=r.read();auto second=r.read();if(first!=second)fail("Variable INFO changed between reads",Availability::transient);return {r.first,std::move(second)};}catch(const VoltageEquationEvidenceError& e){if(e.kind!=Availability::transient||attempt)throw;if(api.reset_transport)api.reset_transport(gpu,DC_TRANSPORT_POWER);}}
}
}
