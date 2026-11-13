// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Explicit read and read-only preview; never applies or refreshes a driver.
#include "core_client.h"
#include <algorithm>
#include <cstdio>
#include <cwchar>
using namespace druta::ui;
namespace {
std::string ascii(const wchar_t* value){std::string out;for(;*value;++value){if(*value<0||*value>127)return {};out.push_back(static_cast<char>(*value));}return out;}
bool matches(const LiveData& data,const std::string& slot,const std::string& uuid){return data.connected&&data.identity.nvapi_ok&&data.identity.nvapi_selected&&!data.identity.pairing_error[0]&&data.identity.slot==slot&&data.identity.uuid==uuid;}
}
int wmain(int argc,wchar_t** argv){
 if(argc!=4){std::puts("Usage: power-weights-read-check CORE_DLL PCI_SLOT EXPECTED_UUID");return 2;}
 const auto slot=ascii(argv[2]),uuid=ascii(argv[3]);auto backend=create_core_backend(argv[1]);std::string error;if(slot.empty()||uuid.empty()||!backend->open(slot,error)){std::printf("Open failed: %s\n",error.c_str());return 2;}
 ProfileJson report={{"slot",slot},{"uuid",uuid},{"hardware_write_requested",false},{"ok",false}};bool ok=false,changed=false;
 try{const auto initial=backend->read(false);if(!matches(initial,slot,uuid))throw std::runtime_error("Expected paired adapter identity mismatch");
  Command command;command.operation=Command::Operation::power_weights_read;const auto read=backend->execute(command);changed|=read.may_have_changed;report["read"]={{"ok",read.ok},{"may_have_changed",read.may_have_changed},{"message",read.message}};
  const auto live=backend->read(false);if(!matches(live,slot,uuid))throw std::runtime_error("Adapter identity changed after relationship read");const auto data=live.power_weights;if(!data)throw std::runtime_error("Relationship data unavailable");
  report["image"]={{"availability",static_cast<int>(data->image_availability)},{"stale",data->image_stale},{"message",data->image_message}};report["info"]={{"availability",static_cast<int>(data->info_availability)},{"stale",data->info_stale},{"message",data->info_message}};report["rows"]=ProfileJson::array();
  for(const auto& row:data->rows){ProfileJson item={{"index",row.index},{"type",row.type},{"target",row.target},{"eligible_for_preview",row.eligible_for_preview},{"stale",row.stale},{"message",row.message}};if(row.weight_raw)item["weight_raw"]=*row.weight_raw;report["rows"].push_back(std::move(item));}
  report["source_references"]=ProfileJson::array();for(const auto& r:data->report.source_references){ProfileJson item={{"policy",r.policy},{"first",r.first},{"last",r.last},{"available",r.available},{"relationships",r.relationships},{"reason",r.reason}};if(r.enabled)item["enabled"]=*r.enabled;report["source_references"].push_back(std::move(item));}
  report["linked_references"]=ProfileJson::array();for(const auto& r:data->report.linked_references){ProfileJson item={{"policy",r.policy},{"relationship",r.relationship},{"available",r.available},{"reason",r.reason}};if(r.target)item["target"]=*r.target;report["linked_references"].push_back(std::move(item));}
  report["range_references"]=ProfileJson::array();for(const auto& r:data->report.range_references)report["range_references"].push_back({{"policy",r.policy},{"first",r.first},{"last",r.last},{"available",r.available},{"relationships",r.relationships},{"reason",r.reason}});
  auto selected=std::find_if(data->rows.begin(),data->rows.end(),[](const auto& r){return r.eligible_for_preview&&!r.stale&&r.weight_raw.has_value();});
  if(selected==data->rows.end())report["preview"]={{"attempted",false},{"message","No current qualified relationship weight available"}};
  else{const uint16_t before=*selected->weight_raw,after=before==65535?uint16_t(before-1):uint16_t(before+1);command.operation=Command::Operation::power_weights_preview;command.power_weight_requests=std::make_shared<const std::vector<PowerWeightRequest>>(std::vector<PowerWeightRequest>{{selected->index,after/4096.0,before}});const auto preview=backend->execute(command);changed|=preview.may_have_changed;report["preview"]={{"attempted",true},{"ok",preview.ok},{"may_have_changed",preview.may_have_changed},{"message",preview.message},{"index",selected->index},{"before_raw",before},{"after_raw",after}};
   const auto fresh=backend->read(false);if(!matches(fresh,slot,uuid))throw std::runtime_error("Adapter identity changed after preview");if(fresh.power_weights&&fresh.power_weights->preview){const auto& p=*fresh.power_weights->preview;report["preview"]["registry_added_count"]=p.registry_added.size();report["preview"]["image_sha256"]=p.image_sha256;report["preview"]["expected_image_sha256"]=p.expected_image_sha256;report["preview"]["registry_added"]=ProfileJson::array();for(const auto& value:p.registry_added)report["preview"]["registry_added"].push_back({{"name",ascii(value.name.c_str())},{"type",value.type},{"bytes",value.bytes.size()}});}}
  ok=read.ok&&!changed;
 }catch(const std::exception& e){report["error"]=e.what();}
 const auto closed=backend->prepare_close();changed|=closed.may_have_changed;report["close"]={{"ok",closed.ok},{"may_have_changed",closed.may_have_changed},{"message",closed.message}};backend->close();report["ok"]=ok&&closed.ok&&!changed;std::puts(report.dump(2).c_str());return report["ok"].get<bool>()?0:1;
}
