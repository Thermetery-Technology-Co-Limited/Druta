// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "profile_catalog_view.h"
#include <imgui.h>
#include <imgui_internal.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
static ProfileCatalogViewOutput frame(ProfileCatalogViewState& state,bool unlocked,bool busy=false,const char* activate=nullptr,const char* library_entry=nullptr){
 ImGui::NewFrame();if(auto* existing=ImGui::FindWindowByName("Profile catalog###druta_profile_catalog"))ImGui::SetWindowSize(existing,{1100,1500},ImGuiCond_Always);if(activate){auto* window=ImGui::FindWindowByName("Profile catalog###druta_profile_catalog");CHECK(window);const auto seed=window->GetID("profile-catalog");const auto id=ImHashStr(activate,0,seed);auto& ctx=*ImGui::GetCurrentContext();ctx.NavActivateId=ctx.NavActivateDownId=ctx.NavActivatePressedId=id;}
 if(library_entry){auto* window=ImGui::FindWindowByName("Profile catalog###druta_profile_catalog");ImGuiWindow* child=nullptr;auto& ctx=*ImGui::GetCurrentContext();for(auto* candidate:ctx.Windows)if(candidate->ParentWindow==window&&std::strstr(candidate->Name,"saved-profile-library")){child=candidate;break;}CHECK(child);const auto scope=ImHashStr(library_entry,0,child->ID);ctx.NavActivateId=ctx.NavActivateDownId=ctx.NavActivatePressedId=ImHashStr("Stage profile",0,scope);}
 auto out=draw_profile_catalog_workspace(state,1,busy,unlocked);ImGui::Render();return out;
}
int main(){ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.f/60;io.DisplaySize={1600,1600};unsigned char* pixels;int w,h;io.Fonts->GetTexDataAsRGBA32(&pixels,&w,&h);
 ProfileCatalogViewState state;state.open=true;auto first=frame(state,false);CHECK(first.intents.size()==1&&first.intents[0].operation==ProfileCatalogIntent::Operation::refresh_library&&state.library_loading);CHECK(frame(state,false).intents.empty());
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
 state.items.clear();sync_profile_catalog_generation(state,1);auto full=ProfileJson{{"schema",2},{"device",{{"uuid","GPU-alternate"},{"slot","0000:04:00.0"}}},{"mem_off_units",400},{"vf_deltas",{{"0",30000}}},{"i2c",{{"profile","RTX 2080 Ti - NVVDD (MP2888A)"},{"sha256","4a3979d6500eca53c3f37e7ccf7f0a1ad2ea71ee819a8e26c11a535c912470f7"},{"port",1},{"addr7",32},{"rail","NVVDD"},{"display_name","offset"},{"offset_mv",12.5}}}};
 add_profile_catalog_item(state,"full.json","Python full tune",full);CHECK(state.items[0].description.valid&&state.items[0].selected_targets.size()==3);CHECK(frame(state,true).intents.empty());
 state.items[0].included=true;state.items[0].selected_targets={"clock:memory"};CHECK(stage_profile_catalog_selection(state)&&state.staged);CHECK((*state.staged)["components"][0]["state"]["source"]==full);
 CHECK(frame(state,true,false,"Apply staged profile").intents.empty());
 // Host supplies completed filesystem reads; the view performs no file I/O.
 record_profile_library(state,{},"Scanned native and Python profile directories");CHECK(!state.library_loading&&state.library_status.find("Scanned")!=std::string::npos);
 out=frame(state,false,false,"Refresh saved profiles");CHECK(out.intents.size()==1&&out.intents[0].operation==ProfileCatalogIntent::Operation::refresh_library&&state.library_loading);CHECK(frame(state,false,false,"Refresh saved profiles").intents.empty());
 ProfileLibraryEntry named;named.id="named.json";named.name="Named memory";named.source="Python profiles";named.saved_at="2026-11-13 12:00";named.document=daily;
 ProfileLibraryEntry automatic=named;automatic.id="autosave-memory.json";automatic.name="autosave-memory-20261113-120000.001";automatic.source="Native history";automatic.autosave=true;
 ProfileLibraryEntry unreadable;unreadable.id="bad-library.json";unreadable.name="Broken profile";unreadable.source="Python profiles";unreadable.error="Invalid JSON";
 record_profile_library(state,{named,automatic,unreadable});CHECK(state.library.size()==3&&!state.library_loading);
 out=frame(state,false,false,"Named");CHECK(out.intents.empty()&&state.library_filter==ProfileLibraryFilter::named);out=frame(state,false,false,"Autosaves");CHECK(out.intents.empty()&&state.library_filter==ProfileLibraryFilter::autosaves);frame(state,false,false,"All");CHECK(state.library_filter==ProfileLibraryFilter::all);
 frame(state,false);out=frame(state,false,false,nullptr,named.id.c_str());CHECK(out.intents.empty()&&state.staged&&!state.preflight_ok);auto staged_before_bad=state.staged;const auto count_before_bad=state.items.size();record_profile_library(state,{unreadable});frame(state,false);out=frame(state,false,false,nullptr,unreadable.id.c_str());CHECK(out.intents.empty()&&state.staged==staged_before_bad&&state.items.size()==count_before_bad);record_profile_library(state,{named,automatic,unreadable});
 const auto prior=state.staged;CHECK(!stage_profile_library_item(state,unreadable.id)&&state.staged==prior);CHECK(stage_profile_library_item(state,automatic.id)&&state.staged&&!state.preflight_ok);CHECK(state.items.back().id==automatic.id&&state.items.back().included);
 CHECK(frame(state,true,false,"Apply staged profile").intents.empty()); // Loading history never bypasses whole preflight.
 record_profile_catalog_preflight(state,1,*state.staged,true,"Reviewed");CHECK(state.preflight_ok);CHECK(stage_profile_library_item(state,named.id)&&!state.preflight_ok);CHECK(frame(state,true,false,"Apply staged profile").intents.empty());
 state.capture_scope=ProfileScope::full_tune;std::snprintf(state.save_name.data(),state.save_name.size(),"Selected full tune");out=frame(state,false,false,"Save named profile");CHECK(out.intents.size()==1&&out.intents[0].operation==ProfileCatalogIntent::Operation::save_named&&out.intents[0].scope==ProfileScope::full_tune&&out.intents[0].name=="Selected full tune"&&out.intents[0].document.is_null());CHECK(frame(state,false,true,"Save named profile").intents.empty());state.save_name[0]=0;CHECK(frame(state,false,false,"Save named profile").intents.empty());
 // History always selects the first autosave before checking whether it can be staged.
 ProfileLibraryEntry newest_bad=unreadable;newest_bad.id="autosave-corrupt";newest_bad.name="autosave-corrupt";newest_bad.autosave=true;
 record_profile_library(state,{named,newest_bad,automatic});state.staged=daily;state.preflight_ok=true;CHECK(!stage_newest_profile_history(state)&&!state.staged&&!state.preflight_ok&&state.status.find("autosave-corrupt")!=std::string::npos&&state.status.find("Invalid JSON")!=std::string::npos);
 ProfileLibraryEntry incomplete=automatic;incomplete.id="autosave-incomplete";incomplete.name="autosave-incomplete";incomplete.document=ProfileJson{{"schema",2},{"device",{{"uuid","GPU-alternate"},{"slot","0000:04:00.0"}}},{"incomplete",ProfileJson::array({"missing curve"})},{"mem_off_units",400}};record_profile_library(state,{incomplete,automatic});CHECK(!stage_newest_profile_history(state)&&!state.staged&&state.status.find("autosave-incomplete")!=std::string::npos);
 record_profile_library(state,{named});CHECK(!stage_newest_profile_history(state)&&!state.staged&&state.status.find("No Undo")!=std::string::npos);
 record_profile_library(state,{named,automatic});CHECK(stage_newest_profile_history(state)&&state.staged&&!state.preflight_ok&&inspect_profile(*state.staged).uuid==std::optional<std::string>("GPU-alternate"));CHECK(frame(state,true,false,"Apply staged profile").intents.empty());
 out=frame(state,true,false,"Undo from history...");CHECK(out.intents.size()==1&&out.intents[0].operation==ProfileCatalogIntent::Operation::stage_newest_history&&!state.staged&&!state.preflight_ok);
 state.open=false;CHECK(frame(state,false).intents.empty());state.open=true;out=frame(state,false);CHECK(out.intents.size()==1&&out.intents[0].operation==ProfileCatalogIntent::Operation::refresh_library);CHECK(frame(state,false).intents.empty());
 ImGui::DestroyContext();std::printf("Profile catalog view: %u checks\n",checks);return 0;
}
