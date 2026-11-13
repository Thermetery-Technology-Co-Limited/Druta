// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_offsets_capture.h"
#include <cstring>
namespace druta::ui {
namespace {
bool same(const dc_target& a,const dc_target& b){return a.size==sizeof(a)&&b.size==sizeof(b)&&std::memchr(a.slot,0,sizeof(a.slot))&&std::memchr(b.slot,0,sizeof(b.slot))&&a.slot[0]&&a.handle&&a.handle==b.handle&&std::strcmp(a.slot,b.slot)==0&&a.architecture==b.architecture&&a.device_id==b.device_id&&a.subsystem_id==b.subsystem_id;}
}
std::function<VoltageOffsetSnapshot(dc_gpu*)> make_voltage_offsets_capture(VoltageEvidenceApi api,const dc_gpu_identity& opened,VoltageScaffoldHostApi host){
 auto scaffold=make_voltage_scaffold_provider(api,opened,std::move(host));
 return [api=std::move(api),opened,scaffold=std::move(scaffold)](dc_gpu* gpu){
  if(!gpu||!api.target.lock||!api.target.unlock)throw VoltageEquationEvidenceError("Offset capture lock interfaces unavailable",VoltagePolicyAvailability::unsupported);
  if(api.target.lock(gpu)!=DC_OK)throw VoltageEquationEvidenceError("Offset capture lock unavailable",VoltagePolicyAvailability::transient);
  struct Unlock{const VoltagePolicyApi& api;dc_gpu* gpu;~Unlock(){api.unlock(gpu);}} unlock{api.target,gpu};
  VoltageOffsetSnapshot result;result.scaffold=scaffold(gpu);
  if(!result.scaffold.equations||!same(result.scaffold.target,result.scaffold.equations->target))throw VoltageEquationEvidenceError("Recognized equations target changed",VoltagePolicyAvailability::invalid);
  result.equations=*result.scaffold.equations;result.retained=capture_voltage_retained_evidence(api,opened,gpu,result.scaffold.target);
  if(!same(result.scaffold.target,result.retained.target)||!result.retained.retained_requests_qualified)throw VoltageEquationEvidenceError("Retained offset evidence target changed",VoltagePolicyAvailability::invalid);
  result.retained.image_sha256=result.scaffold.image_sha256;
  return result;
 };
}
}
