// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Headless integration of the actual Advanced pages; no backend or GPU.
#include "../ui_view.h"
#include "../session.h"
#include "../classic_section.h"
#include "imgui.h"
#include "imgui_internal.h"
#include "implot.h"
#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>
using namespace druta::ui;
namespace {
int failures=0;
void check(bool ok,const char* message){if(!ok){++failures;std::printf("FAIL: %s\n",message);}}
std::string frame(ViewState& state){
    auto& io=ImGui::GetIO();io.DisplaySize={1920,1600};ImGui::NewFrame();ImGui::LogToBuffer();draw(state);
    std::string text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::Render();return text;
}
ImGuiWindow* advanced_window(Presentation presentation){
    auto* root=ImGui::FindWindowByName("Druta workspace");if(!root)return nullptr;
    ImGuiWindow* workspace=nullptr;
    for(auto* w:ImGui::GetCurrentContext()->Windows)
        if(w->ParentWindow==root && w->ChildId==root->GetID("workspace"))workspace=w;
    if(presentation==Presentation::focused)return workspace;
    if(workspace)for(auto* w:ImGui::GetCurrentContext()->Windows)
        if(w->ParentWindow==workspace && w->ChildId==workspace->GetID("classic-advanced-columns"))return w;
    return nullptr;
}
ImGuiID child_id(ImGuiWindow* w,const char* scope,const char* label){return ImHashStr(label,0,ImHashStr(scope,0,w->IDStack[0]));}
void open_header(ImGuiWindow* w,const char* scope,const char* label){
    const auto id=scope?child_id(w,scope,label):w->GetID(label);w->StateStorage.SetInt(id,1);
}
void activate(ViewState& state,ImGuiID id,Command::Operation expected,const char* why){
    state.commands.clear();ImGui::ActivateItemByID(id);frame(state);
    check(state.commands.size()==1 && state.commands[0].operation==expected,why);
}
void pages(Presentation presentation){
    ViewState state;state.preview=false;state.page=Page::advanced;state.presentation=presentation;
    auto live=std::make_shared<SessionSnapshot>();live->generation=1;live->selected_slot="0000:09:00.0";
    live->data.connected=true;std::strcpy(live->data.identity.uuid,"GPU-alternate");state.live=live;
    auto text=frame(state);auto* w=advanced_window(presentation);
    auto* root=ImGui::FindWindowByName("Druta workspace");
    check(root!=nullptr,"profile host exists");
    if(root){
        if(presentation==Presentation::classic){
            // Classic follows 1.9.1: no header profile row; the native catalog
            // is an entry of the menu bar's Profiles menu.
            ImGui::ActivateItemByID(ImHashStr("Profiles",0,root->GetID("##MenuBar")));frame(state);
            const auto& stack=ImGui::GetCurrentContext()->OpenPopupStack;
            auto* menu=stack.empty()?nullptr:stack.back().Window;
            check(menu!=nullptr,"Classic Profiles menu opens");
            if(menu){ImGui::ActivateItemByID(menu->GetID("Profile catalog..."));text=frame(state);}
        }else{
            ImGui::ActivateItemByID(root->GetID("Profile catalog..."));text=frame(state);
        }
        check(state.profile_catalog_view.open && text.find("Choose profile files...")!=std::string::npos,"header opens actual catalog in both skins");
        check(state.commands.empty() && state.profile_catalog_intents.size()==1 &&
            state.profile_catalog_intents.front().operation==ProfileCatalogIntent::Operation::refresh_library,
            "opening catalog requests only filesystem discovery, no hardware action");
        state.profile_catalog_intents.clear();
        state.profile_catalog_view.open=false;text=frame(state);
    }
    check(w!=nullptr,"Advanced content window exists");if(!w)return;
    const bool classic=presentation==Presentation::classic;
    if(classic)check(text.find("Power policies")!=std::string::npos && text.find("Live voltage controls")!=std::string::npos && text.find("Diagnostics")!=std::string::npos,"Classic Advanced exposes the released sections");
    else check(text.find("Power feedback ratios")!=std::string::npos && text.find("Voltage admission and V/F policies")!=std::string::npos && text.find("Power arbitration")!=std::string::npos,"Advanced exposes all service headers");
    check(state.commands.empty(),"drawing does not dispatch commands");
    state.voltage_tables_view.initialized=true;state.voltage_tables_view.generation=1;
    state.voltage_tables_view.drafts.push_back({});state.voltage_tables_view.drafts.back().dirty=true;std::strcpy(state.voltage_tables_view.drafts.back().text.data(),"17.25");
    state.voltage_limits_view.initialized=true;state.voltage_limits_view.generation=1;state.voltage_limits_view.drafts.push_back({});state.voltage_limits_view.drafts.back().dirty=true;state.voltage_limits_view.drafts.back().value=321;
    state.open_voltage_tables_requested=true;text=frame(state);
    check(!state.open_voltage_tables_requested&&text.find(classic?"Read requests":"Read voltage requests")!=std::string::npos,"table navigation opens the actual panel");
    check(state.commands.empty()&&state.voltage_tables_intents.empty()&&state.voltage_limits_intents.empty()&&state.voltage_tables_view.drafts[0].dirty&&std::strcmp(state.voltage_tables_view.drafts[0].text.data(),"17.25")==0,"table navigation preserves drafts and issues no commands");
    state.open_voltage_limits_requested=true;text=frame(state);
    check(!state.open_voltage_limits_requested&&text.find(classic?"Read device delays":"Read voltage device delays")!=std::string::npos,"definitions navigation opens the actual panel");
    check(state.commands.empty()&&state.voltage_tables_intents.empty()&&state.voltage_limits_intents.empty()&&state.voltage_limits_view.drafts[0].dirty&&state.voltage_limits_view.drafts[0].value==321,"definitions navigation preserves drafts and issues no commands");
    state.power_saving_view.initialized=true;state.power_saving_view.generation=1;
    state.power_saving_view.drafts.push_back({});state.power_saving_view.drafts.back().dirty=true;std::strcpy(state.power_saving_view.drafts.back().text.data(),"4000");
    state.clock_gating_view.initialized=true;state.clock_gating_view.generation=1;state.clock_gating_view.pcie_dirty=true;state.clock_gating_view.pcie_generation=2;
    state.open_power_saving_requested=true;text=frame(state);
    check(!state.open_power_saving_requested&&text.find(classic?"Idle threshold (\xC2\xB5s)":"Read power saving")!=std::string::npos,"saving navigation opens the actual panel");
    check(state.commands.empty()&&state.power_saving_intents.empty()&&state.clock_gating_intents.empty()&&state.power_saving_view.drafts[0].dirty&&std::strcmp(state.power_saving_view.drafts[0].text.data(),"4000")==0,"saving navigation preserves drafts and issues no commands");
    state.open_clock_gating_requested=true;text=frame(state);
    check(!state.open_clock_gating_requested&&text.find(classic?"Save PCIe speed profile":"Read clock gating / PCIe")!=std::string::npos,"clock navigation opens the actual panel");
    check(state.commands.empty()&&state.power_saving_intents.empty()&&state.clock_gating_intents.empty()&&state.clock_gating_view.pcie_dirty&&state.clock_gating_view.pcie_generation==2,"clock navigation preserves drafts and issues no commands");
    state.open_voltage_policy_requested=true;text=frame(state);check(!state.open_voltage_policy_requested&&text.find(classic?"Read voltage-request gate":"Read voltage policies")!=std::string::npos,"policy navigation remains available");
    state.open_power_integral_requested=true;text=frame(state);check(!state.open_power_integral_requested&&text.find(classic?"Upper adjustment (%)":"Read feedback requests")!=std::string::npos&&state.commands.empty(),"integral navigation remains command-free");
    ImGuiID feedback=0,voltage=0;
    if(classic){
        // Released 1.9.1 nesting: each panel opens under its section.
        for(const char* label:{"Power policies","Live voltage controls","Runtime voltage-request control","Diagnostics","Power/current arbitration"})open_header(w,nullptr,label);
        feedback=child_id(w,"power_integral_panel","Read");
        int part=static_cast<int>(VoltagePolicyPart::admission);
        voltage=ImHashStr("Read voltage-request gate",0,ImHashData(&part,sizeof(part),ImHashStr("voltage-policy",0,w->IDStack[0])));
    }else{
        open_header(w,"advanced-semantic-controls","Power feedback ratios");
        open_header(w,"advanced-semantic-controls","Voltage admission and V/F policies");
        open_header(w,nullptr,"Power arbitration");
        const auto sem=ImHashStr("advanced-semantic-controls",0,w->IDStack[0]);
        feedback=ImHashStr("Read feedback requests",0,ImHashStr("power_integral_panel",0,sem));
        voltage=ImHashStr("Read voltage policies",0,ImHashStr("voltage-policy",0,sem));
    }
    text=frame(state);
    check(text.find(classic?"Upper adjustment (%)":"Read feedback requests")!=std::string::npos && text.find(classic?"Read arbitration":"Refresh power arbitration")!=std::string::npos && text.find(classic?"Read voltage-request gate":"Read voltage policies")!=std::string::npos,"expanded panels draw actual read widgets");
    check(state.power_policies_open==classic,"only Classic's open Power policies section asks for the policy refresh; Focused never does");
    const auto arbitration=child_id(w,"power-arbitration",classic?"Read arbitration":"Refresh power arbitration");
    check(feedback!=arbitration && feedback!=voltage && voltage!=arbitration,"service read widgets have distinct IDs");
    activate(state,feedback,Command::Operation::power_integral_read,"feedback read dispatches exactly once");
    activate(state,arbitration,Command::Operation::refresh_power_arbitration,"arbitration refresh dispatches exactly once");
    activate(state,voltage,Command::Operation::voltage_policy_read,"voltage read dispatches exactly once");
    for(int mode=0;mode<2;++mode){live->busy=mode==0;live->data.connected=mode!=1;state.commands.clear();
        ImGui::ActivateItemByID(feedback);frame(state);ImGui::ActivateItemByID(arbitration);frame(state);ImGui::ActivateItemByID(voltage);frame(state);
        check(state.commands.empty(),"busy or disconnected disables service reads");}
}
// Released 1.9.1 druta.py build_advanced and registry_panel.py SECTIONS: five
// closed sections in reading order, each holding its own subsections.
void classic_hierarchy(){
    ViewState state;state.preview=false;state.page=Page::advanced;state.presentation=Presentation::classic;
    auto live=std::make_shared<SessionSnapshot>();live->generation=1;live->selected_slot="0000:41:00.0";
    live->data.connected=true;std::strcpy(live->data.identity.uuid,"GPU-hierarchy");state.live=live;
    auto text=frame(state);auto* w=advanced_window(Presentation::classic);
    check(w!=nullptr,"classic Advanced window exists");if(!w)return;
    w->StateStorage.Clear();text=frame(state);  // forget headers the earlier page test opened
    const char* sections[]={"Power policies","Registry / BIOS overrides","Live voltage controls","Live power-saving & PCIe","Diagnostics"};
    const std::vector<std::vector<const char*>> subs{
        {"Power-policy feedback adjustment"},
        {"Pascal clock ceilings and ratios","Power relationship weights","DIFR configured state"},
        {"Runtime voltage-request control","Runtime VF policy control","Voltage policy offsets","Live voltage rail requests",
         "Live REL equation bounds","Live voltage constants","Voltage switch-delay requests","Undo, recovery and profiles"},
        {"Live engine-level power gating (ELPG)","Individual engine power gating","Live idle thresholds","Live adaptive power policy",
         "Live engine clock gating","Live block clock gating","Live second-level clock gating (SLCG)","Live DIFR disable request","Manual PCIe speed request",
         "Undo, recovery and profiles"},
        {"Driver policy diagnostics","Power/current arbitration"}};
    size_t at=0;bool ordered=true,hidden=true;
    for(const char* section:sections){const auto pos=text.find(section,at);ordered=ordered&&pos!=std::string::npos;if(pos!=std::string::npos)at=pos;}
    for(const auto& group:subs)for(const char* sub:group)hidden=hidden&&text.find(sub)==std::string::npos;
    check(ordered,"closed page lists the five released sections in order");
    check(hidden,"subsections stay hidden while their section is closed");
    for(const char* old:{"Native workflow pending","Clock gating and PCIe speed","Voltage request and offset tables","PERF diagnostics","Power feedback ratios"})
        check(text.find(old)==std::string::npos,"flat native headers no longer appear at top level");
    for(const char* section:sections)open_header(w,nullptr,section);
    text=frame(state);at=0;ordered=true;
    for(size_t i=0;i<5;++i){
        const auto pos=text.find(sections[i],at);if(pos==std::string::npos){ordered=false;break;}at=pos;
        const auto next=i+1<5?text.find(sections[i+1],at):std::string::npos;
        for(const char* sub:subs[i]){const auto found=text.find(sub,at);ordered=ordered&&found!=std::string::npos&&found<next;if(found!=std::string::npos)at=found;}
    }
    check(ordered,"each subsection is drawn inside its own section, in released order");
    // registry_panel.py _section/_sub: default_open=False at every level, so an opened section
    // shows only its closed sub-bars (Power policies draws its feedback adjustment inline).
    for(const char* body:{"Read driver policies","Read arbitration","Read relationships","Read clock table","Read voltage-request gate","Read offsets","Read requests",
        "Read device delays","Hold disabled","Hold disable request","Request speed","Idle threshold (\xC2\xB5s)"})
        check(text.find(body)==std::string::npos,"opening a section leaves every subsection closed");
    check(text.find("This driver session only. A GPU restart returns these to the driver's values.")!=std::string::npos,"session-only scope note is shown");
    check(text.find("Read-only.")!=std::string::npos,"Diagnostics states it is read-only");
    check(text.find("Power relationship weights persist until removed")!=std::string::npos&&text.find("Pascal clock settings last for the driver session")!=std::string::npos,"registry section describes the two override lifetimes");
    check(text.find("does not install them")!=std::string::npos,"voltage section states its offset exception");
    check(state.commands.empty()&&state.power_saving_intents.empty()&&state.clock_gating_intents.empty()&&state.voltage_tables_intents.empty()&&state.voltage_limits_intents.empty(),"opening sections dispatches nothing");
    // power_integral_panel.py: the feedback adjustment sits inline at the end of Power
    // policies under a separator and plain heading, not in a collapsed subsection.
    check(text.find("Upper adjustment (%)")!=std::string::npos&&text.find("Lower adjustment (%)")!=std::string::npos,"feedback adjustment is drawn as soon as Power policies opens");
    // No Session controls block and no adapter identity block on the released page.
    check(text.find("Session controls")==std::string::npos&&text.find("Adapter and driver")==std::string::npos&&text.find("Refresh details")==std::string::npos,"Classic Advanced has no native Session controls or identity block");
    check(text.find("Every section states whether its changes last for this driver session or persist across GPU restarts.")!=std::string::npos,"the page opens with the released scope line");
    // Actions spanning every family are drawn once, in the shared subsection, not
    // under each family heading.
    const auto count=[&](const char* label){size_t n=0;for(auto pos=text.find(label);pos!=std::string::npos;pos=text.find(label,pos+1))++n;return n;};
    for(const char* sub:{"Live engine-level power gating (ELPG)","Individual engine power gating","Live idle thresholds","Live adaptive power policy",
        "Live engine clock gating","Live block clock gating","Live second-level clock gating (SLCG)","Live DIFR disable request","Manual PCIe speed request",
        "Runtime voltage-request control","Runtime VF policy control","Live REL equation bounds","Live voltage constants","Voltage switch-delay requests",
        "Voltage policy offsets","Live voltage rail requests",
        "Undo, recovery and profiles##live-voltage","Undo, recovery and profiles##live-saving"})open_header(w,nullptr,sub);
    text=frame(state);
    for(const char* shared:{"Undo power-saving request","Release owned power-saving requests","Undo clock / PCIe requests","Load clock profile...",
        "Undo voltage limits","Recover selected voltage limit","Undo voltage policy","Load policy profile...",
        "Undo installed offsets","Recover installed offsets","Load requests profile","Undo voltage requests","Recover selected voltage requests"})
        check(count(shared)==1,"an action spanning every family is drawn once with all subsections open");
    check(text.find("Save request profile")!=std::string::npos&&text.find("Read voltage-request gate")!=std::string::npos&&text.find("Read device delays")!=std::string::npos,"family subsections keep their own reads");
    // The voltage tables' chosen-subset save (the Focused table's Include) stays reachable once, folded.
    check(count("Save a selection of offsets")==1&&count("Save a selection of rail requests")==1,"Classic keeps one subset save for offsets and one for rail requests");
    check(state.commands.empty()&&state.power_saving_intents.empty()&&state.clock_gating_intents.empty()&&state.voltage_limits_intents.empty(),"opening every subsection dispatches nothing");
    // Navigation from the Clocks menu opens the path to one panel and closes the rest.
    state.open_power_weights_requested=true;text=frame(state);
    check(!state.open_power_weights_requested&&text.find("Power relationship weights")!=std::string::npos&&text.find("Live REL equation bounds")==std::string::npos,"weights navigation opens Registry / BIOS overrides only");
    live->data.clock_architecture=4;
    state.open_pascal_clocks_requested=true;text=frame(state);
    check(text.find("Read clock table")!=std::string::npos&&text.find("Run Druta as administrator")!=std::string::npos,
          "Pascal clock navigation allows non-admin inspection and explains restart requirements");
    live->data.clock_architecture=7;text=frame(state);
    check(text.find("Read clock table")==std::string::npos,"another architecture does not inherit the Pascal controls");
    state.open_clock_gating_requested=true;text=frame(state);
    check(text.find("Manual PCIe speed request")!=std::string::npos&&text.find("Power-policy feedback adjustment")==std::string::npos,"gating navigation opens Live power-saving & PCIe only");
}
// Released 1.9.1 build_power_policy_section and the adv_status mirror line.
PowerPolicy policy(uint32_t id,const char* unit,double current,double low,double high,bool writable,const char* detail){
    PowerPolicy p;p.id=id;p.label="Policy "+std::to_string(id);p.binding="binding-"+std::to_string(id);p.channel=id+1;p.policy_type=5;
    p.state.availability=Availability::available;p.state.current=current;p.state.low=low;p.state.high=high;p.state.stock=current;
    p.state.unit=unit;p.state.step=0.001;p.state.writable=writable;p.state.stock_writable=writable;p.state.detail=detail;return p;
}
void classic_power_policies(){
    ViewState state;state.preview=false;state.page=Page::advanced;state.presentation=Presentation::classic;state.controls_unlocked=true;
    auto live=std::make_shared<SessionSnapshot>();live->generation=3;live->selected_slot="0000:21:00.0";
    live->data.connected=true;std::strcpy(live->data.identity.uuid,"GPU-policies");
    live->data.power_policies.push_back(policy(0,"W",350,100,450,false,"Board request is controlled separately"));
    live->data.power_policies.push_back(policy(2,"W",150,50,200,true,"Configured policy request"));
    auto current=policy(4,"A",30,10,40,true,"Configured policy request");current.observed_amps=12.5;current.limit_amps=20;
    live->data.power_policies.push_back(current);
    auto core=policy(6,"A",300,100,400,true,"Configured policy request");core.core_current=true;core.named_current=true;
    live->data.power_policies.push_back(core);
    live->log.push_back({1,"curve read: peak 2115 MHz",true});
    state.live=live;
    auto text=frame(state);auto* w=advanced_window(Presentation::classic);
    check(w!=nullptr,"classic Advanced window exists for policies");if(!w)return;
    w->StateStorage.Clear();text=frame(state);
    check(text.find("[ok] curve read: peak 2115 MHz")!=std::string::npos,"the newest log line is mirrored with its [ok] tag");
    {   // druta.py log(): adv_status shows the newest Classic log line, an untagged one DIM, an [ok] one GOOD.
        const auto vertices=[](ImU32 colour){size_t n=0;const auto* dd=ImGui::GetDrawData();
            for(int i=0;i<dd->CmdListsCount;++i)for(const auto& v:dd->CmdLists[i]->VtxBuffer)if(v.col==colour)++n;return n;};
        const ImU32 dim=IM_COL32(139,144,153,255),good=IM_COL32(70,209,122,255);
        const auto dim_tagged=vertices(dim),good_tagged=vertices(good);
        state.classic_log_lines.push_back({1,"backend: NVAPI: ok   |   NVML: ok",std::nullopt});text=frame(state);
        check(text.find("backend: NVAPI: ok   |   NVML: ok")!=std::string::npos&&text.find("[ok] curve read")==std::string::npos
            &&vertices(dim)>dim_tagged&&vertices(good)<good_tagged,"a newer untagged Classic line replaces it and is drawn DIM");
        state.classic_log_lines.clear();text=frame(state);
    }
    // refresh_power_policies reads only while the header is open: the closed section
    // asks the host for no policy-table refresh.
    check(!state.power_policies_open,"a closed Power policies section asks for no policy refresh");
    open_header(w,nullptr,"Power policies");text=frame(state);
    check(state.power_policies_open&&state.commands.empty(),"an open Power policies section asks for the read-only refresh and queues nothing");
    state.page=Page::daily;frame(state);
    check(!state.power_policies_open,"another page asks for no policy refresh");
    state.page=Page::advanced;text=frame(state);
    check(text.find("Beware of your PSU's rating as you change power limit.")!=std::string::npos&&text.find("Max all")!=std::string::npos,"PSU warning and Max all head the section");
    check(text.find("connector shows its current per 12 V wire")!=std::string::npos,"the released explanation follows the warning");
    check(text.find("set with Power limit (W) on the Control tab")!=std::string::npos,"the board policy is read-only and says where it is set");
    check(text.find("set with its current-limit slider on the Control tab")!=std::string::npos,"the core current policy says it is set on the Control tab");
    const auto count=[&](const char* label){size_t n=0;for(auto pos=text.find(label);pos!=std::string::npos;pos=text.find(label,pos+1))++n;return n;};
    check(count("policy 2, ch 3, 0x05, W")==1&&count("policy 4, ch 5, 0x05, A")==1,"each policy row is drawn once, with its released description");
    check(text.find("12.5 A")!=std::string::npos&&text.find("\xE2\x89\xA4" "20 A")!=std::string::npos,"a current row shows the live reading and its effective limit, not the request");
    check(text.find("Session controls")==std::string::npos&&text.find("Current and power policies")==std::string::npos,"no second policy table is drawn");
    check(text.find("Policy workspace...")!=std::string::npos,"the native workspace stays reachable inside Power policies");
    state.commands.clear();ImGui::ActivateItemByID(w->GetID("Max all"));frame(state);frame(state);
    bool applies=false;for(const auto& c:state.commands)applies=applies||c.operation==Command::Operation::apply||c.operation==Command::Operation::apply_scoped_profile;
    check(state.policy_workspace.open&&state.policy_workspace.selected.size()==4,"Max all opens the workspace with every policy selected");
    check(!applies&&state.commands.size()==1&&state.commands[0].operation==Command::Operation::prepare_scoped_profile,"Max all only re-reads the requests; nothing is applied without review");
    state.controls_unlocked=false;state.commands.clear();state.policy_workspace.open=false;
    ImGui::ActivateItemByID(w->GetID("Max all"));frame(state);frame(state);
    check(state.commands.empty()&&!state.policy_workspace.open,"Max all needs unlocked controls, as Python's guard does");
}
// Released 1.9.1 ui_sections.section: level colours, 18 px nesting indent, 8/2 px spacer.
void section_style(){
    ImGui::GetIO().DisplaySize={800,600};ImGui::NewFrame();ImGui::Begin("sections");ImGui::PushFont(nullptr,16.0f);
    const auto same=[](ImVec4 c,int r,int g,int b){return std::abs(c.x*255-r)<0.5f&&std::abs(c.y*255-g)<0.5f&&std::abs(c.z*255-b)<0.5f;};
    const float spacing=ImGui::GetStyle().ItemSpacing.y;const ImVec4 base=ImGui::GetStyleColorVec4(ImGuiCol_Header);
    float y=ImGui::GetCursorPosY();ImGui::CollapsingHeader("plain");const float plain=ImGui::GetCursorPosY()-y;
    float x0=0,x1=0,x2=0;ImVec4 h0,h1,body;
    y=ImGui::GetCursorPosY();
    {ClassicSection top{"Top",0};x0=ImGui::GetItemRectMin().x;h0=ImGui::GetStyleColorVec4(ImGuiCol_Header);}
    check(std::abs(ImGui::GetCursorPosY()-y-(plain+8+spacing))<0.01f,"a top-level section is followed by an 8 px spacer");
    const float x_before=ImGui::GetCursorPosX();y=ImGui::GetCursorPosY();
    {ClassicSection nested{"Nested",1};x1=ImGui::GetItemRectMin().x;h1=ImGui::GetStyleColorVec4(ImGuiCol_Header);}
    check(std::abs(ImGui::GetCursorPosY()-y-(plain+2+spacing))<0.01f,"a subsection is followed by a 2 px spacer");
    check(std::abs(ImGui::GetCursorPosX()-x_before)<0.01f,"the indent ends with the subsection");
    check(std::abs(x1-x0-18)<0.01f,"a subsection bar is indented 18 px");
    check(same(h0,38,66,104)&&same(h1,46,50,58),"each level uses its released header colour");
    ImGui::SetNextItemOpen(true);
    {ClassicSection open{"Open",1,"scope"};if(open){body=ImGui::GetStyleColorVec4(ImGuiCol_Header);
        ImGui::SetNextItemOpen(true);ClassicSection deeper{"Deeper",2};x2=ImGui::GetItemRectMin().x;}}
    check(same(body,46,50,58),"a section body keeps its level's header colour for plain headers inside it");
    check(std::abs(x2-x1-18)<0.01f,"a third-level section indents again under its parent");
    const ImVec4 after=ImGui::GetStyleColorVec4(ImGuiCol_Header);
    check(after.x==base.x&&after.y==base.y&&after.z==base.z,"sections restore the header colour");
    // Dear PyGui add_text: with Classic's stock metrics (FramePadding 4,3; ItemSpacing 8,4) each text
    // item sits on a 22 px frame-height line, so an open section's note, its explainer and its
    // status line are 26 px apart, where plain ImGui text gives 20. A wrapped item keeps the font pitch inside.
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding,{4,3});ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,{8,4});
    y=ImGui::GetCursorPosY();ImGui::CollapsingHeader("stock plain");const float stock_plain=ImGui::GetCursorPosY()-y;
    y=ImGui::GetCursorPosY();ImGui::SetNextItemOpen(true);
    {ClassicSection noted{"Noted",1,"scope note"};
        check(std::abs(ImGui::GetCursorPosY()-y-(stock_plain+26))<0.01f,"an open section's scope note takes a 26 px add_text line");
        float at=ImGui::GetCursorPosY();classic_text("explainer");
        check(std::abs(ImGui::GetCursorPosY()-at-26)<0.01f&&std::abs(ImGui::GetItemRectMin().y-ImGui::GetWindowPos().y+ImGui::GetScrollY()-at-3)<0.01f,"an explainer sits 3 px down a 22 px line, 26 px before the next item");
        at=ImGui::GetCursorPosY();classic_text("status line");check(std::abs(ImGui::GetCursorPosY()-at-26)<0.01f,"the status line follows on the same 26 px pitch");
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX()+40);at=ImGui::GetCursorPosY();classic_text("a wrapped explainer that takes several lines");ImGui::PopTextWrapPos();
        const float wrapped=ImGui::GetCursorPosY()-at;check(wrapped>26&&std::abs(std::fmod(wrapped-3-4,16.0f))<0.01f,"a wrapped item keeps the font pitch between its own lines");}
    ImGui::PopStyleVar(2);
    ImGui::PopFont();ImGui::End();ImGui::Render();
}
}
int main(){ImGui::CreateContext();ImPlot::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.0f/60.0f;
    unsigned char* pixels=nullptr;int width=0,height=0;io.Fonts->GetTexDataAsRGBA32(&pixels,&width,&height);apply_theme(1);
    pages(Presentation::focused);pages(Presentation::classic);classic_hierarchy();classic_power_policies();section_style();ImPlot::DestroyContext();ImGui::DestroyContext();
    std::printf("Advanced view integration: %d failures\n",failures);return failures?1:0;}
