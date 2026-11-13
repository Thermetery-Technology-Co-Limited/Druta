// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "profile_catalog_view.h"
#include <imgui.h>
#include <imgui_internal.h>
#include <cstdio>
#include <cstdlib>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
static ProfileCatalogViewOutput frame(ProfileCatalogViewState& state,bool unlocked,bool busy=false,const char* activate=nullptr){
 ImGui::NewFrame();if(activate){auto* window=ImGui::FindWindowByName("Profile catalog###druta_profile_catalog");CHECK(window);const auto seed=window->GetID("profile-catalog");const auto id=ImHashStr(activate,0,seed);auto& ctx=*ImGui::GetCurrentContext();ctx.NavActivateId=ctx.NavActivateDownId=ctx.NavActivatePressedId=id;}
 auto out=draw_profile_catalog_workspace(state,1,busy,unlocked);ImGui::Render();return out;
}
int main(){ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.f/60;io.DisplaySize={1600,1600};unsigned char* pixels;int w,h;io.Fonts->GetTexDataAsRGBA32(&pixels,&w,&h);
 ProfileCatalogViewState state;state.open=true;CHECK(frame(state,false).intents.empty());
 auto out=frame(state,false,false,"Choose profile files...");CHECK(out.intents.size()==1&&out.intents[0].operation==ProfileCatalogIntent::Operation::choose_files);
 state.capture_scope=ProfileScope::timing;out=frame(state,false,false,"Save configured scope...");CHECK(out.intents.size()==1&&out.intents[0].scope==ProfileScope::timing);CHECK(frame(state,false,true,"Save configured scope...").intents.empty());
 ProfileJson daily={{"format","druta-native-daily"},{"version",1},{"target",{{"uuid","GPU-alternate"},{"slot","0000:04:00.0"}}},{"controls",{{"memory",{{"value",400},{"unit","MHz"},{"scale",2}}}}}};
 add_profile_catalog_item(state,"one.json","Memory",daily);CHECK(state.items.size()==1&&state.items[0].description.valid&&!state.items[0].included&&!state.staged);
 state.items[0].included=true;CHECK(stage_profile_catalog_selection(state)&&state.staged&&!state.preflight_ok);const auto staged=*state.staged;
 out=frame(state,false,false,"Save composed profile...");CHECK(out.intents.size()==1&&out.intents[0].document==staged);
 out=frame(state,false,false,"Preflight staged profile");CHECK(out.intents.size()==1&&out.intents[0].operation==ProfileCatalogIntent::Operation::preflight);
 CHECK(frame(state,true,false,"Apply staged profile").intents.empty());record_profile_catalog_preflight(state,1,staged,true,"Whole preflight passed");CHECK(state.preflight_ok);
 CHECK(frame(state,false,false,"Apply staged profile").intents.empty());CHECK(frame(state,true,true,"Apply staged profile").intents.empty());
 out=frame(state,true,false,"Apply staged profile");CHECK(out.intents.size()==1&&out.intents[0].operation==ProfileCatalogIntent::Operation::apply&&out.intents[0].document==staged&&!state.preflight_ok);
 add_profile_catalog_item(state,"two.json","Duplicate",daily);state.items[1].included=true;CHECK(!stage_profile_catalog_selection(state)&&!state.staged&&state.status.find("overlapping")!=std::string::npos);
 state.items[1].included=false;CHECK(stage_profile_catalog_selection(state));const auto current=*state.staged;add_profile_catalog_item(state,"one.json","Updated",daily);CHECK(state.items.size()==2&&!state.staged);
 record_profile_catalog_preflight(state,1,current,true,"Stale completion");CHECK(!state.preflight_ok);state.items[0].included=true;CHECK(stage_profile_catalog_selection(state));
 record_profile_catalog_preflight(state,0,*state.staged,true,"Wrong generation");CHECK(!state.preflight_ok);
 sync_profile_catalog_generation(state,2);CHECK(!state.staged&&!state.preflight_ok&&!state.items[0].included&&state.items.size()==2);
 add_profile_catalog_item(state,"bad.json","Unsupported",{{"scope","mystery"},{"schema",2}});CHECK(!state.items.back().description.valid);state.items.back().included=true;CHECK(!stage_profile_catalog_selection(state));
 state.items.clear();sync_profile_catalog_generation(state,1);auto full=ProfileJson{{"schema",2},{"device",{{"uuid","GPU-alternate"},{"slot","0000:04:00.0"}}},{"mem_off_units",400},{"vf_deltas",{{"0",30000}}},{"i2c",{{"offset_mv",10}}}};
 add_profile_catalog_item(state,"full.json","Python full tune",full);CHECK(state.items[0].description.valid&&state.items[0].selected_targets.size()==3);CHECK(frame(state,true).intents.empty());
 state.items[0].included=true;state.items[0].selected_targets={"clock:memory"};CHECK(stage_profile_catalog_selection(state)&&state.staged);CHECK((*state.staged)["components"][0]["state"]["source"]==full);
 CHECK(frame(state,true,false,"Apply staged profile").intents.empty());
 ImGui::DestroyContext();std::printf("Profile catalog view: %u checks\n",checks);return 0;
}
