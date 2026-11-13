// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_weights_service.h"
#include "../src/core/sha256.h"
#include <algorithm>
#include <atomic>
#include <cstring>
#include <cwctype>
#include <map>
#include <stdexcept>
#include <tuple>
namespace druta::ui {
namespace {
using Availability=VoltagePolicyAvailability;
[[noreturn]]void fail(const std::string& s,Availability kind=Availability::invalid){throw VoltageEquationEvidenceError(s,kind);}
void require(bool ok,const char* s){if(!ok)fail(s);}
template<size_t N>std::string text(const char(&s)[N]){const auto* end=static_cast<const char*>(std::memchr(s,0,N));require(end,"Power weight adapter identity string malformed");return {s,end};}
bool same_target(const dc_target& a,const dc_target& b){return a.size==sizeof(a)&&b.size==sizeof(b)&&text(a.slot)==text(b.slot)&&a.handle&&a.handle==b.handle&&a.architecture==b.architecture&&a.device_id==b.device_id&&a.subsystem_id==b.subsystem_id;}
bool same_layout(const PowerRelationshipLayout& a,const PowerRelationshipLayout& b){return a.info_command==b.info_command&&a.info_size==b.info_size&&a.policy_base==b.policy_base&&a.policy_stride==b.policy_stride;}
bool same_windows(const std::optional<VoltageScaffoldWindowsTarget>& a,const std::optional<VoltageScaffoldWindowsTarget>& b){return a.has_value()==b.has_value()&&(!a||(a->slot==b->slot&&a->instance_id==b->instance_id&&a->driver_key==b->driver_key));}
using RegistryMap=std::map<std::wstring,std::pair<uint32_t,std::vector<uint8_t>>>;
RegistryMap registry_map(const PowerWeightRegistrySnapshot& snapshot){RegistryMap out;for(const auto& value:snapshot){require(value.name.find(L'\0')==std::wstring::npos,"Registry name contains NUL");auto folded=value.name;for(auto& c:folded)c=static_cast<wchar_t>(std::towlower(c));require(out.emplace(std::move(folded),std::make_pair(value.type,value.bytes)).second,"Registry names conflict case-insensitively");}return out;}
bool same_registry(const std::optional<PowerWeightRegistrySnapshot>& a,const std::optional<PowerWeightRegistrySnapshot>& b){return a.has_value()==b.has_value()&&(!a||registry_map(*a)==registry_map(*b));}
std::string hash(const std::vector<uint8_t>& b){return druta::sha256_hex(b.data(),b.size());}
}
struct PowerWeightsPreview::State {
 std::shared_ptr<int> session;mutable std::atomic_bool valid{true};
 PowerWeightCapture original;PowerWeightPlan plan;PowerWeightRegistryProjection registry;
 bool no_change=false;
};
struct PowerWeightsService::Impl {
 PowerWeightsApi api;dc_gpu* gpu;dc_gpu_identity opened;PowerWeightsData data;
 std::shared_ptr<int> session=std::make_shared<int>(0);
 bool registry_known=false;
 Impl(PowerWeightsApi a,dc_gpu* g,const dc_gpu_identity& id):api(std::move(a)),gpu(g),opened(id){data.identity=id;}
 void cancel_preview(){if(data.preview&&data.preview->state)data.preview->state->valid=false;data.preview.reset();}
 void binding(const PowerWeightCapture& c){const auto& id=c.adapter;
  require(opened.size==sizeof(opened)&&id.size==sizeof(id)&&id.nvapi_ok&&id.nvapi_selected&&!text(opened.slot).empty()&&text(id.slot)==text(opened.slot)&&text(id.uuid)==text(opened.uuid)&&text(opened.pairing_error).empty()&&text(id.pairing_error).empty()&&id.nvapi_handle==opened.nvapi_handle&&id.nvapi_devid==opened.nvapi_devid&&id.nvapi_subsys==opened.nvapi_subsys&&id.nvml_selected==opened.nvml_selected&&id.nvml_has_devid==opened.nvml_has_devid&&id.nvml_devid==opened.nvml_devid&&id.nvml_subsys==opened.nvml_subsys,"Power weight selected adapter identity changed");
  require(c.target.size==sizeof(c.target)&&c.target.handle==id.nvapi_handle&&c.target.handle&&text(c.target.slot)==text(id.slot)&&c.target.device_id==id.nvapi_devid&&c.target.subsystem_id==id.nvapi_subsys&&c.identity.slot==text(id.slot)&&c.identity.architecture==c.target.architecture&&c.identity.device_id==c.target.device_id&&c.identity.subsystem_id==c.target.subsystem_id,"Power weight capture target differs from paired adapter");
 }
 PowerWeightCapture capture(bool registry){if(!api.capture)fail("Power weight capture interface unavailable",Availability::unsupported);for(unsigned attempt=0;attempt<2;++attempt)try{auto c=api.capture(gpu,registry);binding(c);return c;}catch(const VoltageEquationEvidenceError& e){if(e.kind!=Availability::transient||attempt)throw;}fail("Power weight bounded capture failed",Availability::transient);}
 PowerWeightCapture sample(bool registry){try{return capture(registry);}catch(const VoltageEquationEvidenceError& e){unavailable(e.kind,e.what());throw;}catch(const std::exception& e){unavailable(Availability::invalid,e.what());throw;}}
 bool same_read(const PowerWeightCapture& a,const PowerWeightCapture& b){return same_target(a.target,b.target)&&same_layout(a.layout,b.layout)&&a.image_availability==Availability::available&&b.image_availability==Availability::available&&a.info_availability==Availability::available&&b.info_availability==Availability::available&&a.image==b.image&&a.info==b.info;}
 void unavailable(Availability kind,const std::string& message){data.image_stale=data.image.ok;data.info_stale=data.report.available;data.image_availability=data.info_availability=kind;data.image_message=data.info_message=message;for(auto& r:data.rows){r.stale=r.weight_raw.has_value();r.eligible_for_preview=false;r.availability=kind;r.message=message;}data.message=message;cancel_preview();++data.revision;}
 void publish(const PowerWeightCapture& c,bool registry){
  if(data.preview&&data.preview->state&&!same_read(data.preview->state->original,c))cancel_preview();
  data.image_availability=c.image_availability;data.info_availability=c.info_availability;data.image_message=c.image_message;data.info_message=c.info_message;
  if(c.image_availability==Availability::available){auto image=inspect_power_weight_image(c.image);if(image.ok){data.image=std::move(image);data.image_stale=false;}else{data.image_availability=Availability::invalid;data.image_message=image.message;data.image_stale=data.image.ok;}}
  else data.image_stale=data.image.ok;
  if(c.info_availability==Availability::available){auto report=decode_power_relationships(c.layout,c.info);if(report.available){data.report=std::move(report);data.info_stale=false;}else{data.info_availability=Availability::invalid;data.info_message=report.reason;data.info_stale=data.report.available;}}
  else data.info_stale=data.report.available;
  if(registry){data.registry_availability=c.registry_availability;data.registry_message=c.registry_message;data.registry_stale=c.registry_availability!=Availability::available&&registry_known;if(c.registry_availability==Availability::available&&c.registry)registry_known=true;}
  if(data.info_availability==Availability::available){std::vector<PowerWeightRow> rows;for(const auto& record:data.report.relationships){PowerWeightRow row;row.index=record.index;row.type=record.type;row.target=record.target;row.weight_raw=record.weight_raw;row.target_metadata=record.target_metadata;
    row.availability=record.weight_raw&&record.target_metadata?Availability::available:Availability::unsupported;row.message=record.target_reason;
    if(data.image_availability==Availability::available&&record.weight_raw){auto plan=plan_power_relationship_weights(c.image,c.identity,c.layout,c.info,{{record.index,double(*record.weight_raw)/4096.0,record.weight_raw}});row.eligible_for_preview=plan.ok;if(!plan.ok)row.message=plan.message;}
    else if(data.image_availability!=Availability::available)row.message=data.image_message.empty()?"Current image unavailable for weight preview":data.image_message;
    rows.push_back(std::move(row));}data.rows=std::move(rows);
  }else for(auto& row:data.rows){row.stale=row.weight_raw.has_value();row.eligible_for_preview=false;row.availability=data.info_availability;row.message=data.info_message;}
  data.message=data.info_availability==Availability::available?"Relationship inspection only; Preview does not write registry or activate weights":"Relationship read unavailable: "+data.info_message;++data.revision;
 }
 void complete(const PowerWeightCapture& c){require(!text(opened.uuid).empty(),"Preview requires the current adapter UUID");require(c.image_availability==Availability::available&&c.info_availability==Availability::available&&c.registry_availability==Availability::available&&c.registry.has_value()&&c.windows.has_value(),"Complete image, INFO and registry evidence required for Preview");require(c.windows->slot==text(c.target.slot)&&!c.windows->instance_id.empty()&&!c.windows->driver_key.empty()&&c.windows->instance_id.find('\0')==std::string::npos&&c.windows->driver_key.find('\0')==std::string::npos,"Preview Windows target incomplete");registry_map(*c.registry);}
};
PowerWeightsService::PowerWeightsService(PowerWeightsApi api,dc_gpu* gpu,const dc_gpu_identity& id):impl_(std::make_unique<Impl>(std::move(api),gpu,id)){}
PowerWeightsService::~PowerWeightsService()=default;
const PowerWeightsData& PowerWeightsService::state()const{return impl_->data;}
const PowerWeightsData& PowerWeightsService::read(){auto& p=*impl_;try{auto current=p.capture(false);p.publish(current,false);}catch(const VoltageEquationEvidenceError& e){p.unavailable(e.kind,e.what());}catch(const std::exception& e){p.unavailable(Availability::invalid,e.what());}return p.data;}
PowerWeightsPreview PowerWeightsService::preview(const std::vector<PowerWeightRequest>& requests){auto& p=*impl_;PowerWeightsPreview out;p.cancel_preview();try{auto current=p.sample(true);p.publish(current,true);p.complete(current);auto plan=plan_power_relationship_weights(current.image,current.identity,current.layout,current.info,requests);require(plan.ok,plan.message.c_str());
  auto state=std::make_shared<PowerWeightsPreview::State>();state->session=p.session;state->original=current;state->plan=plan;state->no_change=plan.patches.empty();
  if(state->no_change)state->registry={*current.registry,current.image};else{std::vector<PowerWeightRegistryPatch> patches;for(const auto& patch:plan.patches)patches.push_back({patch.offset,patch.before,patch.after});const auto image=inspect_power_weight_image(current.image);state->registry=project_power_weight_registry(*current.registry,current.image,image.selector_version,image.selector_oem,patches);}
  require(plan.expected_image&&state->registry.expected_image==*plan.expected_image,"Projected registry image differs from selected weight plan");out.no_change=state->no_change;out.selected=plan.selected;out.windows=*current.windows;out.image_sha256=hash(current.image);out.expected_image_sha256=hash(state->registry.expected_image);
  const auto originals=registry_map(*current.registry);for(const auto& value:state->registry.after){auto folded=value.name;for(auto& c:folded)c=static_cast<wchar_t>(std::towlower(c));if(!originals.count(folded))out.registry_added.push_back(value);}
  out.state=std::move(state);out.ok=true;out.message=out.no_change?"Selected weights already match; no registry changes":"Selected exact weight spans and registry additions previewed; no writes or activation";
 }catch(const std::exception& e){out.message=e.what();}p.data.preview=std::make_shared<const PowerWeightsPreview>(out);p.data.message=out.message;++p.data.revision;return out;}
PowerWeightsResult PowerWeightsService::validate_preview(const PowerWeightsPreview& preview){auto& p=*impl_;const auto retained_state=preview.state;PowerWeightsResult result;try{require(retained_state&&retained_state->session==p.session&&retained_state->valid,"Preview is invalid or belongs to another service generation");const auto& saved=*retained_state;auto current=p.sample(true);p.publish(current,true);p.complete(current);require(saved.valid&&p.same_read(saved.original,current)&&same_windows(saved.original.windows,current.windows)&&same_registry(saved.original.registry,current.registry),"Preview evidence changed; create a fresh Preview");result.ok=true;result.message="Original target, complete image, INFO and registry still match Preview; no activation performed";
 }catch(const std::exception& e){if(retained_state&&retained_state->session==p.session)retained_state->valid=false;p.cancel_preview();result.message=e.what();}p.data.message=result.message;++p.data.revision;return result;}
}
