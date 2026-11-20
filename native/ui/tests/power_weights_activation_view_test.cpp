// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_weights_view.h"
#include "imgui_internal.h"
#include <cstdio>
using namespace druta::ui;
namespace {
unsigned checks=0,failures=0;
#define CHECK(x) do{++checks;if(!(x)){++failures;std::fprintf(stderr,"Activation view line %d: %s\n",__LINE__,#x);}}while(false)
using Operation=PowerWeightsIntent::Operation;
std::vector<PowerWeightsIntent> activation_frame(PowerWeightsViewState& view,PowerWeightsData& data,bool classic,bool busy=false,bool unlocked=true,bool connected=true,const char* click=nullptr){
 auto& io=ImGui::GetIO();io.DisplaySize={1600,1400};
 // ImGui consumes the queued navigation activation during NewFrame.
 if(click){auto* window=ImGui::FindWindowByName("Weight activation test");CHECK(window!=nullptr);if(window){const auto scope=ImHashStr("power-weights",0,window->ID);ImGui::ActivateItemByID(ImHashStr(click,0,scope));}}
 ImGui::NewFrame();ImGui::SetNextWindowPos({0,0});ImGui::SetNextWindowSize({1500,1300});ImGui::Begin("Weight activation test",nullptr,ImGuiWindowFlags_NoSavedSettings);auto intents=draw_power_weights(view,&data,12,busy,classic,unlocked,connected);ImGui::End();ImGui::Render();return intents;
}
bool only(const std::vector<PowerWeightsIntent>& intents,Operation operation){return intents.size()==1&&intents[0].operation==operation&&intents[0].requests.empty();}
PowerWeightsData activation_data(){PowerWeightsData data;PowerWeightRow row;row.index=2;row.target=4;row.weight_raw=4096;row.eligible_for_preview=true;row.availability=VoltagePolicyAvailability::available;data.rows.push_back(row);auto preview=std::make_shared<PowerWeightsPreview>();preview->ok=true;preview->message="Qualified selected preview";PowerWeightSelection selected;selected.index=2;selected.before_raw=4096;selected.after_raw=5120;preview->selected.push_back(selected);data.preview=std::move(preview);return data;}
void activation_guards(bool classic){
 PowerWeightsViewState view;auto data=activation_data();CHECK(activation_frame(view,data,classic).empty());
 const char* apply="Apply preview and refresh GPU";
 CHECK(only(activation_frame(view,data,classic,false,true,true,apply),Operation::prepare));
 CHECK(activation_frame(view,data,classic,false,false,true,apply).empty());
 CHECK(activation_frame(view,data,classic,true,true,true,apply).empty());
 CHECK(activation_frame(view,data,classic,false,true,false,apply).empty());
 auto noop=std::make_shared<PowerWeightsPreview>(*data.preview);noop->no_change=true;data.preview=noop;
 CHECK(activation_frame(view,data,classic,false,true,true,apply).empty());
 auto changed=std::make_shared<PowerWeightsPreview>(*noop);changed->no_change=false;data.preview=changed;
 auto invalid=std::make_shared<PowerWeightsPreview>(*changed);invalid->ok=false;data.preview=invalid;CHECK(activation_frame(view,data,classic,false,true,true,apply).empty());data.preview=changed;
 // Read/Preview require a live connection; metadata inspection remains read-only.
 view.drafts[0].include=true;view.drafts[0].dirty=true;view.drafts[0].multiplier=1.25;
 CHECK(activation_frame(view,data,classic,false,true,false,"Read relationships").empty());
 CHECK(activation_frame(view,data,classic,false,true,false,"Preview selected weights").empty());
 CHECK(only(activation_frame(view,data,classic,false,false,false,"Inspect saved override"),Operation::inspect));
 CHECK(activation_frame(view,data,classic,true,true,true,"Inspect saved override").empty());
 // A prepared receipt owns activation admission; the old preview cannot prepare another.
 auto receipt=std::make_shared<PowerWeightReceipt>();receipt->id=std::string(32,'a');receipt->phase="prepared";data.receipt=receipt;
 CHECK(activation_frame(view,data,classic,false,true,true,apply).empty());
 const char* activate="Activate saved preview and refresh GPU";
 CHECK(only(activation_frame(view,data,classic,false,true,true,activate),Operation::activate_saved));
 CHECK(activation_frame(view,data,classic,false,false,true,activate).empty());
 CHECK(activation_frame(view,data,classic,true,true,true,activate).empty());
 CHECK(activation_frame(view,data,classic,false,true,false,activate).empty());
 const char* verify="Verify saved override";const char* remove="Remove override and refresh GPU";
 CHECK(only(activation_frame(view,data,classic,false,false,true,verify),Operation::verify));
 CHECK(only(activation_frame(view,data,classic,false,false,false,verify),Operation::verify));
 CHECK(activation_frame(view,data,classic,true,false,false,verify).empty());
 CHECK(only(activation_frame(view,data,classic,false,true,false,remove),Operation::remove));
 CHECK(activation_frame(view,data,classic,false,false,false,remove).empty());
 CHECK(activation_frame(view,data,classic,true,true,false,remove).empty());
 // Uncertain or already-issued prepared records are recovery-only, never fresh activation.
 receipt->uncertain=true;
 CHECK(activation_frame(view,data,classic,false,true,true,activate).empty());
 CHECK(only(activation_frame(view,data,classic,false,false,false,verify),Operation::verify));
 CHECK(only(activation_frame(view,data,classic,false,true,false,remove),Operation::remove));
 receipt->uncertain=false;receipt->issued_names={L"romOverride00"};
 CHECK(activation_frame(view,data,classic,false,true,true,activate).empty());
 receipt->issued_names.clear();receipt->phase="active";
 CHECK(activation_frame(view,data,classic,false,true,true,activate).empty());
 CHECK(activation_frame(view,data,classic,false,true,true,apply).empty());
 CHECK(only(activation_frame(view,data,classic,false,false,false,verify),Operation::verify));
 CHECK(only(activation_frame(view,data,classic,false,true,false,remove),Operation::remove));
 receipt->phase="remove_refresh_pending";receipt->restoring=true;receipt->uncertain=true;
 CHECK(activation_frame(view,data,classic,false,true,true,activate).empty());
 CHECK(only(activation_frame(view,data,classic,false,false,false,verify),Operation::verify));
 CHECK(only(activation_frame(view,data,classic,false,true,false,remove),Operation::remove));
 // Completed removal disables old receipt actions and allows a new changed Preview.
 receipt->phase="removed";receipt->uncertain=false;
 CHECK(activation_frame(view,data,classic,false,true,true,verify).empty());
 CHECK(activation_frame(view,data,classic,false,true,false,remove).empty());
 CHECK(activation_frame(view,data,classic,false,true,true,activate).empty());
 CHECK(only(activation_frame(view,data,classic,false,true,true,apply),Operation::prepare));
 data.receipt.reset();data.preview.reset();CHECK(activation_frame(view,data,classic,false,true,true,apply).empty());CHECK(activation_frame(view,data,classic,false,true,false,remove).empty());
}
}
int main(){ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.f/60;unsigned char* pixels=nullptr;int width=0,height=0;io.Fonts->GetTexDataAsRGBA32(&pixels,&width,&height);for(bool classic:{false,true})activation_guards(classic);ImGui::DestroyContext();std::printf("Power weights activation view: %u checks, %u failures\n",checks,failures);return failures?1:0;}
