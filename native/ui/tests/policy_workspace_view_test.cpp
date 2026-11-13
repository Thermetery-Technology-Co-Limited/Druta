// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "policy_workspace_view.h"
#include "imgui.h"
#include "imgui_internal.h"
#include <cstdio>
using namespace druta::ui;
int failures=0;void check(bool v,const char* s){if(!v){++failures;std::printf("FAIL: %s\n",s);}}
PolicyWorkspaceOutput frame(PolicyWorkspaceViewState& v,PolicyAnnotations& a,const ScopedSnapshot* s,bool busy=false,bool connected=true,bool unlocked=true){
 ImGui::GetIO().DisplaySize={1200,900};ImGui::NewFrame();auto o=draw_policy_workspace(v,a,s,1,busy,connected,unlocked);ImGui::Render();return o;
}
int main(){ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.0f/60;unsigned char* pixels;int w,h;io.Fonts->GetTexDataAsRGBA32(&pixels,&w,&h);
 ScopedSnapshot s;s.target={"GPU-X","0000:09:00.0"};ScopedCurrent p;p.key=ScopedKey::policy_request(7);p.binding="valid";p.unit="mW";p.value=100000;p.low=10000;p.high=200000;p.fresh=p.writable=true;s.entries.push_back(p);
 PolicyAnnotations a;PolicyWorkspaceViewState state;state.open=true;state.generation=1;state.selected.push_back(p.key);
 auto o=frame(state,a,&s);check(!o.refresh&&!o.apply&&!o.save_annotations,"drawing never dispatches");
 auto* window=ImGui::FindWindowByName("Policy workspace###druta_policy_workspace");check(window!=nullptr,"workspace exists");
 if(window){ImGui::ActivateItemByID(window->GetID("Preview selected maxima"));o=frame(state,a,&s);check(state.preview&&state.preview->document&&!o.apply,"preview remains local");
 ImGui::ActivateItemByID(window->GetID("Apply reviewed maxima"));o=frame(state,a,&s,true);check(!o.apply,"busy disables apply");
 ImGui::ActivateItemByID(window->GetID("Apply reviewed maxima"));o=frame(state,a,&s,false,true,false);check(!o.apply,"locked disables apply");
 ImGui::ActivateItemByID(window->GetID("Apply reviewed maxima"));o=frame(state,a,&s);check(o.apply.has_value()&&!state.preview,"explicit apply emits immutable reviewed document once");
 ImGui::ActivateItemByID(window->GetID("Preview selected maxima"));frame(state,a,&s);s.entries[0].high=190000;frame(state,a,&s);check(!state.preview,"changed limit invalidates review");}
 ImGui::DestroyContext();std::printf("Policy workspace view: %d failures\n",failures);return failures?1:0;
}
