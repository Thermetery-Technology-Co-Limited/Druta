// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_weights_view.h"
#include "imgui_internal.h"
#include <cstdio>
#include <cstdlib>
#include <string>
using namespace druta::ui;
#define CHECK(x) do{if(!(x)){std::fprintf(stderr,"%d: %s\n",__LINE__,#x);std::abort();}}while(false)
std::vector<PowerWeightsIntent> frame(PowerWeightsViewState& v,PowerWeightsData& d,uint64_t generation,bool busy,bool classic,const char* click=nullptr){auto& io=ImGui::GetIO();io.DisplaySize={1600,1200};if(click){auto* w=ImGui::FindWindowByName("Weights test");CHECK(w);auto id=ImHashStr("power-weights",0,w->ID);ImGui::ActivateItemByID(ImHashStr(click,0,id));}ImGui::NewFrame();ImGui::SetNextWindowSize({1400,1000});ImGui::Begin("Weights test");auto out=draw_power_weights(v,&d,generation,busy,classic);ImGui::End();ImGui::Render();return out;}
void firmware_policy_view(bool classic) {
 PowerWeightsData data;
 PowerWeightsViewState view;
 PowerRelationshipTarget definition{5,11,9,1,1000,25000,45000};
 data.image.ok=true;
 data.image.policies.push_back({5,10,definition,true,{}});
 data.report.available=true;
 data.report.policies_available=true;
 data.report.policy_mask=1u<<5;
 data.report.policies[5]=definition;
 const auto render=[&] {
  ImGui::GetIO().DisplaySize={1600,1200};
  ImGui::NewFrame();
  ImGui::SetNextWindowSize({1400,1000});
  ImGui::Begin("Power definition evidence");
  ImGui::PushID("power-weights");
  ImGui::GetStateStorage()->SetInt(ImGui::GetID("BIOS power and current definitions"),1);
  ImGui::PopID();
  ImGui::LogToBuffer();
  const auto intents=draw_power_weights(view,&data,1,false,classic);
  const std::string shown=ImGui::GetCurrentContext()->LogBuffer.c_str();
  ImGui::LogFinish();
  ImGui::End();
  ImGui::Render();
  CHECK(intents.empty());
  return shown;
 };
 const auto fresh=render();
 CHECK(fresh.find("45 A")!=std::string::npos && fresh.find("Matches driver definition")!=std::string::npos);
 data.info_stale=true;
 const auto stale=render();
 CHECK(stale.find("Awaiting fresh comparison")!=std::string::npos && stale.find("Matches driver definition")==std::string::npos);
}
int main(){ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.f/60;unsigned char* pixels;int w,h;io.Fonts->GetTexDataAsRGBA32(&pixels,&w,&h);
 firmware_policy_view(false);
 firmware_policy_view(true);
 for(bool classic:{false,true}){PowerWeightsViewState view;PowerWeightsData data;PowerWeightRow row;row.index=4;row.target=2;row.weight_raw=4096;row.eligible_for_preview=true;data.rows.push_back(row);CHECK(frame(view,data,1,false,classic).empty()&&view.drafts.size()==1&&view.drafts[0].expected==4096);
 auto& draft=view.drafts[0];draft.include=true;draft.dirty=true;draft.multiplier=1.5;data.rows[0].weight_raw=5000;auto intent=frame(view,data,1,false,classic,"Preview selected weights");CHECK(intent.size()==1&&intent[0].operation==PowerWeightsIntent::Operation::preview&&intent[0].requests.size()==1&&intent[0].requests[0].expected_raw==4096&&draft.multiplier==1.5);
 draft.multiplier=16;CHECK(frame(view,data,1,false,classic,"Preview selected weights").empty());draft.multiplier=-0.01;CHECK(frame(view,data,1,false,classic,"Preview selected weights").empty());draft.multiplier=65535.0/4096;CHECK(frame(view,data,1,false,classic,"Preview selected weights").size()==1);
 data.rows[0].stale=true;CHECK(frame(view,data,1,false,classic,"Preview selected weights").empty());data.rows[0].stale=false;CHECK(frame(view,data,1,true,classic,"Preview selected weights").empty()&&frame(view,data,1,true,classic,"Read relationships").empty());CHECK(frame(view,data,1,false,classic,"Read relationships").size()==1);frame(view,data,2,false,classic);CHECK(!view.drafts[0].dirty&&!view.drafts[0].include&&view.drafts[0].expected==5000);}
 // Released 1.9.1 relationship_lines: the read-only Diagnostics text.
 {CHECK(power_relationships_text(nullptr).empty());  // Python's box is empty before a read
  PowerWeightsData data;std::snprintf(data.identity.slot,sizeof(data.identity.slot),"0000:05:00.0");
  data.report.reason="INFO packet unavailable";CHECK(power_relationships_text(&data)=="Configured relationships read unavailable: INFO packet unavailable");
  data.report.available=true;
  PowerRelationshipSource source;source.policy=1;source.available=true;source.enabled=true;source.relationships={3,4};data.report.source_references.push_back(source);
  PowerRelationshipLink link;link.policy=1;link.relationship=3;link.available=true;link.target=7;data.report.linked_references.push_back(link);
  PowerRelationshipRecord record;record.index=3;record.type=0;record.target=7;record.weight_raw=6144;
  record.target_metadata=PowerRelationshipTarget{7,2,9,0,100000,350000,0xFFFFFFFFu};data.report.relationships.push_back(record);
  PowerRelationshipRecord other;other.index=4;other.type=2;other.target=8;other.target_reason="Target policy absent";data.report.relationships.push_back(other);
  const auto text=power_relationships_text(&data);
  CHECK(text.find("Configured relationships for adapter 0000:05:00.0\n")==0);
  CHECK(text.find("Source policy 1 -> configured relationships: 3, 4")!=std::string::npos);
  CHECK(text.find("Source policy 1 -> configured relationship 3 -> target policy 7")!=std::string::npos);
  CHECK(text.find("Relationship 3 -> target policy 7 (type 0)\n  Weight: 1.5 (raw 6144)")!=std::string::npos);
  CHECK(text.find("  Target policy type 2, channel 9: minimum 100 W; nominal 350 W; maximum unavailable (sentinel)")!=std::string::npos);
  CHECK(text.find("Relationship 4 -> target policy 8 (type 2)\n  Relationship type is uninterpreted.\n  Target details unavailable: Target policy absent")!=std::string::npos);
  CHECK(text.find("No configured relationships.")==std::string::npos);
  // Failure reasons from the real decoder read as power_relationships.py words them.
  {std::vector<uint8_t> info(20512);const auto put32=[&](size_t at,uint32_t v){for(int i=0;i<4;++i)info[at+i]=uint8_t(v>>(8*i));};
   const auto put16=[&](size_t at,uint16_t v){info[at]=uint8_t(v);info[at+1]=uint8_t(v>>8);};
   put32(4,(1u<<1)|(1u<<2));put32(0x41cc,1u<<3);const size_t rel=0x41d0+0x24*3;info[rel]=0;info[rel+1]=7;  // target 7 is not in the policy mask
   const size_t linked=0xcc+0x104*1;info[linked]=0x30;info[linked+4]=32;put16(linked+0x34,9);         // relationship 9 is not in the INFO mask
   const size_t range=0xcc+0x104*2;info[range]=0x30;info[range+0xb9]=1;put16(range+0xbc,3);put16(range+0xbe,3);
   PowerWeightsData failed;failed.report=decode_power_relationships({},info);CHECK(failed.report.available);
   const auto t=power_relationships_text(&failed);
   CHECK(t.find("Source policy 2: unavailable (A referenced relationship or target is unavailable.)")!=std::string::npos);
   CHECK(t.find("Source policy 1 -> configured relationship 9: unavailable (The linked relationship is absent from the INFO mask.)")!=std::string::npos);
   CHECK(t.find("  Target details unavailable: The target policy is absent from the INFO mask.")!=std::string::npos);
   CHECK(t.find("unrecognized")==std::string::npos&&t.find(" unavailable)")==std::string::npos);
   info.pop_back();failed.report=decode_power_relationships({},info);
   CHECK(power_relationships_text(&failed)=="Configured relationships read unavailable: The policy INFO packet is invalid or incomplete.");}
  // Classic leaves the configured references to that read-only view.
  PowerWeightsViewState view;ImGui::GetIO().DisplaySize={1600,1200};ImGui::NewFrame();ImGui::LogToBuffer();ImGui::Begin("Weights refs");
  draw_power_weights(view,&data,1,false,true);const std::string classic_text=ImGui::GetCurrentContext()->LogBuffer.c_str();
  ImGui::LogFinish();ImGui::End();ImGui::Render();CHECK(classic_text.find("Configured relationship references")==std::string::npos);}
 ImGui::DestroyContext();std::puts("Power weights view passed");}
