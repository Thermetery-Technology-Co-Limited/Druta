// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_weights_view.h"
#include "imgui_internal.h"
#include <cstdio>
#include <cstdlib>
using namespace druta::ui;
#define CHECK(x) do{if(!(x)){std::fprintf(stderr,"%d: %s\n",__LINE__,#x);std::abort();}}while(false)
std::vector<PowerWeightsIntent> frame(PowerWeightsViewState& v,PowerWeightsData& d,uint64_t generation,bool busy,bool classic,const char* click=nullptr){auto& io=ImGui::GetIO();io.DisplaySize={1600,1200};if(click){auto* w=ImGui::FindWindowByName("Weights test");CHECK(w);auto id=ImHashStr("power-weights",0,w->ID);ImGui::ActivateItemByID(ImHashStr(click,0,id));}ImGui::NewFrame();ImGui::SetNextWindowSize({1400,1000});ImGui::Begin("Weights test");auto out=draw_power_weights(v,&d,generation,busy,classic);ImGui::End();ImGui::Render();return out;}
int main(){ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.f/60;unsigned char* pixels;int w,h;io.Fonts->GetTexDataAsRGBA32(&pixels,&w,&h);
 for(bool classic:{false,true}){PowerWeightsViewState view;PowerWeightsData data;PowerWeightRow row;row.index=4;row.target=2;row.weight_raw=4096;row.eligible_for_preview=true;data.rows.push_back(row);CHECK(frame(view,data,1,false,classic).empty()&&view.drafts.size()==1&&view.drafts[0].expected==4096);
 auto& draft=view.drafts[0];draft.include=true;draft.dirty=true;draft.multiplier=1.5;data.rows[0].weight_raw=5000;auto intent=frame(view,data,1,false,classic,"Preview selected weights");CHECK(intent.size()==1&&intent[0].operation==PowerWeightsIntent::Operation::preview&&intent[0].requests.size()==1&&intent[0].requests[0].expected_raw==4096&&draft.multiplier==1.5);
 draft.multiplier=16;CHECK(frame(view,data,1,false,classic,"Preview selected weights").empty());draft.multiplier=-0.01;CHECK(frame(view,data,1,false,classic,"Preview selected weights").empty());draft.multiplier=65535.0/4096;CHECK(frame(view,data,1,false,classic,"Preview selected weights").size()==1);
 data.rows[0].stale=true;CHECK(frame(view,data,1,false,classic,"Preview selected weights").empty());data.rows[0].stale=false;CHECK(frame(view,data,1,true,classic,"Preview selected weights").empty()&&frame(view,data,1,true,classic,"Read relationships").empty());CHECK(frame(view,data,1,false,classic,"Read relationships").size()==1);frame(view,data,2,false,classic);CHECK(!view.drafts[0].dirty&&!view.drafts[0].include&&view.drafts[0].expected==5000);}
 ImGui::DestroyContext();std::puts("Power weights view passed");}
