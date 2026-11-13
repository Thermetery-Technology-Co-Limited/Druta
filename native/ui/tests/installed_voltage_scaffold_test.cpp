// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "installed_voltage_scaffold.h"
#define main unused_voltage_graph_fixture_main
#include "vbios_voltage_graph_test.cpp"
#undef main
namespace {
struct Case {std::vector<uint8_t> image,variables;VoltageEquationPackets before;};
Case scaffold_case(bool object=false,uint8_t variable=7,unsigned free=2){
 Case c;c.image=fixture(object,0x4567);const auto vfe=object?object_vfe:legacy_vfe,base=vfe+relative_base;
 const std::vector<uint8_t> terms(c.image.begin()+base+150,c.image.begin()+base+150+80);
 const std::array<uint8_t,25> empty={0,255,255,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,255};
 for(unsigned i=0;i<free;++i)std::copy(empty.begin(),empty.end(),c.image.begin()+base+25*(6+i));
 std::copy(terms.begin(),terms.end(),c.image.begin()+base+25*(6+free));c.image[vfe+5]=uint8_t(6+free);
 for(unsigned i=1;i<6;++i)c.image[base+25*i+1]=variable;
 if(object)dword(c.image,0x2210+120+8,relative_base+25*(6+free)+80);
 c.before.target.size=sizeof(dc_target);std::strcpy(c.before.target.slot,"0000:04:00.0");c.before.target.handle=42;c.before.target.architecture=7;c.before.target.device_id=0x4567;c.before.target.subsystem_id=0x12345678;
 c.before.info.resize(0xe0408);c.before.control.resize(0x100408);c.before.info[0]=c.before.control[0]=1;
 const auto graph=parse_vbios_voltage_graph(c.image);for(uint32_t i=0;i<6;++i){const auto& row=graph.equations[i];const auto at=0x408+0x70*i,ct=0x408+0x80*i;
  c.before.info[4+i/8]|=uint8_t(1u<<(i%8));c.before.control[4+i/8]|=uint8_t(1u<<(i%8));c.before.info[at]=c.before.info[at+0x6c]=c.before.control[ct]=c.before.control[ct+0x7c]=i==0?6:3;
  word(c.before.info,at+2,row.next);c.before.info[at+0x6d]=row.variable;c.before.info[at+0x6e]=2;number(c.before.control,ct+4,row.lower);number(c.before.control,ct+8,row.upper);
  if(i){number(c.before.control,ct+0x10,row.coefficients[0]);}c.before.info[at+30]=0xa3;c.before.control[ct+0x60]=0x9d;
 }
 c.variables.resize(0xc364);c.variables[4+variable/8]|=uint8_t(1u<<(variable%8));return c;
}
VoltageEquationPackets scaffold_loaded(const Case& c,const InstalledVoltageScaffoldPlan& plan,int64_t value=0){auto loaded=c.before;
 for(const auto& head:plan.heads)for(const bool info:{true,false}){auto& b=info?loaded.info:loaded.control;const uint32_t stride=info?0x70u:0x80u,at=0x408+head.index*stride;
  std::copy_n(b.begin()+0x408+stride,stride,b.begin()+at);b[4+head.index/8]|=uint8_t(1u<<(head.index%8));if(info)word(b,at+2,head.next);else for(const auto offset:{4u,8u,16u})number(b,at+offset,static_cast<float>(value));}
 return loaded;
}
void scaffold_tests(){
 const std::vector<std::pair<uint32_t,VbiosVoltageRole>> selected={{0,VbiosVoltageRole::rel},{0,VbiosVoltageRole::alt}};
 for(const bool object:{false,true}){auto c=scaffold_case(object);const auto original=c.image;const auto before=c.before.control;auto plan=plan_installed_voltage_scaffold(c.image,c.before,c.variables,selected);
  check(plan.ok&&plan.heads.size()==2&&plan.heads[0].index==6&&plan.heads[1].index==7&&plan.heads[0].next==0&&plan.heads[1].next==1,"Canonical ordered independent heads planned on alternate legacy/EFI layouts");
  check(plan.ok&&plan.heads[0].variable==7&&c.image==original&&c.before.control==before,"Template variable retained and inputs never mutated");if(!plan.ok)continue;
  const auto base=(object?object_vfe:legacy_vfe)+relative_base;check((*plan.planned_image)[base+25+2]==0,"Original ALT chain still points at original REL");
  auto loaded=scaffold_loaded(c,plan);auto recognized=recognize_installed_voltage_scaffold(plan,*plan.planned_image,loaded);
  check(recognized.ok&&recognized.complete_image_match&&recognized.heads.size()==2&&recognized.heads[0].value_uv==0,"Installed zero heads recognized with exact original packet preservation");
  loaded=scaffold_loaded(c,plan,-5000);recognized=recognize_installed_voltage_scaffold(plan,*plan.planned_image,loaded);check(recognized.ok&&recognized.heads[0].value_uv==-5000,"Signed exact live offset distinct from planned initial zero");
  auto changed=loaded;changed.info[0x408+2*0x70+30]^=1;check(!recognize_installed_voltage_scaffold(plan,*plan.planned_image,changed).ok,"Unknown original INFO bytes preserved exactly");
  changed=loaded;changed.control[0x408+0x80*2+0x60]^=1;check(!recognize_installed_voltage_scaffold(plan,*plan.planned_image,changed).ok,"Unknown original CONTROL bytes preserved exactly");
  changed=loaded;changed.info[0x404]^=1;check(!recognize_installed_voltage_scaffold(plan,*plan.planned_image,changed).ok,"Nonmask header unknown bytes preserved independently");
  changed=loaded;changed.target.handle=43;check(!recognize_installed_voltage_scaffold(plan,*plan.planned_image,changed).ok,"Changed target handle rejected");
  changed=loaded;word(changed.info,0x408+6*0x70+2,2);check(!recognize_installed_voltage_scaffold(plan,*plan.planned_image,changed).ok,"Forged head next link rejected");
  changed=loaded;number(changed.control,0x408+6*0x80+4,0.5f);number(changed.control,0x408+6*0x80+8,0.5f);number(changed.control,0x408+6*0x80+16,0.5f);check(!recognize_installed_voltage_scaffold(plan,*plan.planned_image,changed).ok,"Fractional signed head values refused");
  changed=loaded;changed.info[4]|=0x80;changed.control[4]|=0x80; // already selected index7; remove a required original instead
  changed.info[4]&=uint8_t(~1u);changed.control[4]&=uint8_t(~1u);check(!recognize_installed_voltage_scaffold(plan,*plan.planned_image,changed).ok,"Missing original inventory rejected");
  auto current=*plan.planned_image;current[0x700]^=1;recognized=recognize_installed_voltage_scaffold(plan,current,loaded);
  check(recognized.ok&&!recognized.complete_image_match&&!recognize_installed_voltage_scaffold(plan,current,loaded,true).ok,"Postactivation recognition permits unrelated image bytes but strict activation requires whole plan");
  current=*plan.planned_image;current[base+25*6+16]^=1;check(!recognize_installed_voltage_scaffold(plan,current,loaded).ok,"Owned VFE bytes outside zero projection must remain exact");
  current=*plan.planned_image;current[rail+5+17]^=1;check(!recognize_installed_voltage_scaffold(plan,current,loaded).ok,"Unselected rail selector must remain exact");
  auto forged=plan;forged.heads[0].next=2;check(recognize_installed_voltage_scaffold(forged,*plan.planned_image,loaded).ok,"Public review projection cannot replace immutable plan identity");
 }
 auto c=scaffold_case(false,11,1);auto plan=plan_installed_voltage_scaffold(c.image,c.before,c.variables,{{0,VbiosVoltageRole::ov}});
 check(plan.ok&&plan.heads[0].variable==11&&plan.heads[0].next==2,"Alternate present variable and one selected role supported");
 check(!plan_installed_voltage_scaffold(c.image,c.before,c.variables,selected).ok,"Insufficient canonical slots rejected");
 auto variables=c.variables;std::fill(variables.begin(),variables.end(),uint8_t{0});check(!plan_installed_voltage_scaffold(c.image,c.before,variables,{{0,VbiosVoltageRole::ov}}).ok,"Template variable must be present in fresh inventory");
 check(!plan_installed_voltage_scaffold(c.image,c.before,c.variables,{{0,VbiosVoltageRole::ov},{0,VbiosVoltageRole::ov}}).ok,"Duplicate selected policies refused");
 check(!plan_installed_voltage_scaffold(c.image,c.before,c.variables,{{1,VbiosVoltageRole::ov}}).ok,"Missing unrelated rail cannot become selected scope");
 check(!plan_installed_voltage_scaffold(c.image,c.before,c.variables,{{0,VbiosVoltageRole::info_vmin}}).ok,"Informational selector is not an offset policy");
 c.image[legacy_vfe+relative_base+25*6+20]=1;check(!plan_installed_voltage_scaffold(c.image,c.before,c.variables,{{0,VbiosVoltageRole::ov}}).ok,"Noncanonical inactive bytes cannot be reused");
 c=scaffold_case(false,7,1);c.image[rail+5+21]=6;check(!plan_installed_voltage_scaffold(c.image,c.before,c.variables,{{0,VbiosVoltageRole::ov}}).ok,"Possible byte alias reserves even inactive canonical slot");
 c=scaffold_case();number(c.before.control,0x408+0x80+4,21000);number(c.before.control,0x408+0x80+8,21000);number(c.before.control,0x408+0x80+16,21000);
 // All other templates are deliberately unbound to prove ALT mismatch is not silently accepted.
 for(uint32_t i=2;i<6;++i)c.before.info[0x408+0x70*i+0x6d]=8;
 check(!plan_installed_voltage_scaffold(c.image,c.before,c.variables,selected).ok,"Constant template equality includes original value and variable binding");
}
}
int main(){scaffold_tests();std::printf("Installed voltage scaffold: %d checks, %d failures\n",checks,failures);return failures?1:0;}
