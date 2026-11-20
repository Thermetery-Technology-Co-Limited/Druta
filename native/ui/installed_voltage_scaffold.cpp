// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Pure port of fa6ee83 src/druta/voltage_offset_scaffold.py plan/verify_loaded.
#include "installed_voltage_scaffold.h"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <set>
#include <stdexcept>
namespace druta::ui {
struct InstalledVoltageScaffoldPlan::State {
 VoltageEquationPackets before;
 std::shared_ptr<const std::vector<uint8_t>> original,planned;
 std::shared_ptr<const VbiosVoltageGraph> graph;
 std::vector<InstalledVoltageScaffoldHead> heads;
 uint32_t vfe_end=0,rail_end=0;
};
namespace {
using Bytes=std::vector<uint8_t>;
void require(bool condition,const char* message){if(!condition)throw std::runtime_error(message);}
void span(const Bytes& b,size_t at,size_t size){require(at<=b.size()&&size<=b.size()-at,"Scaffold evidence extent is not captured");}
uint32_t u32(const Bytes& b,size_t at){span(b,at,4);return uint32_t(b[at])|uint32_t(b[at+1])<<8|uint32_t(b[at+2])<<16|uint32_t(b[at+3])<<24;}
uint16_t u16(const Bytes& b,size_t at){span(b,at,2);return uint16_t(b[at]|uint16_t(b[at+1])<<8);}
float f32(const Bytes& b,size_t at){const auto bits=u32(b,at);float value;std::memcpy(&value,&bits,4);return value;}
void word(Bytes& b,size_t at,uint32_t value){span(b,at,2);b[at]=uint8_t(value);b[at+1]=uint8_t(value>>8);}
bool active(const Bytes& info,uint32_t index){return index<8192&&(info[4+index/8]&(1u<<(index%8)));}
bool target_equal(const dc_target& a,const dc_target& b){return a.size==sizeof(a)&&b.size==sizeof(b)&&a.handle&&a.handle==b.handle&&std::memchr(a.slot,0,sizeof(a.slot))&&std::memchr(b.slot,0,sizeof(b.slot))&&a.slot[0]&&std::strcmp(a.slot,b.slot)==0&&a.architecture==b.architecture&&a.device_id==b.device_id&&a.subsystem_id==b.subsystem_id;}
void shape(const VoltageEquationPackets& packets){require(packets.info.size()==0xe0408&&packets.control.size()==0x100408,"Complete native equation INFO/CONTROL required");require(target_equal(packets.target,packets.target),"Complete equation target identity required");
 require(packets.info[0]==packets.control[0]&&std::equal(packets.info.begin()+4,packets.info.begin()+0x404,packets.control.begin()+4),"Equation INFO/CONTROL inventory differs");}
uint32_t selector(VbiosVoltageRole role){switch(role){case VbiosVoltageRole::rel:return 13;case VbiosVoltageRole::alt:return 15;case VbiosVoltageRole::ov:return 17;case VbiosVoltageRole::vmin:return 19;default:throw std::runtime_error("Unknown scaffold policy role");}}
void own(const Bytes& b,const VbiosVoltageGraph& g,uint32_t at,uint32_t size,std::optional<uint32_t> expected={}){
 span(b,at,size);const auto legacy=std::find_if(g.images.begin(),g.images.end(),[](const auto& image){return image.start==0&&image.type==0&&image.vendor==0x10de;});require(legacy!=g.images.end(),"Legacy image ownership unavailable");unsigned owners=0;uint32_t id=0;
 if(!g.objects.empty()&&at>=legacy->size){for(const auto& object:g.objects)if(object.offset<=at&&uint64_t(at)+size<=uint64_t(object.offset)+object.size){++owners;id=object.id;}require(owners==1&&(!expected||id==*expected),"Scaffold table lacks expected object owner");}
 else{for(const auto& image:g.images)if(image.type!=3&&image.start<=at&&uint64_t(at)+size<=uint64_t(image.start)+image.size)++owners;require(owners==1,"Scaffold table crosses image ownership boundary");}
}
struct Constant {int64_t value=0;uint32_t next=0,variable=0;};
Constant constant(const VoltageEquationPackets& packets,uint32_t index){require(active(packets.info,index),"Constant head absent from loaded inventory");const size_t at=0x408+0x70*index,ct=0x408+0x80*index;
 require(packets.info[at]==3&&packets.info[at+0x6c]==3&&packets.control[ct]==3&&packets.control[ct+0x7c]==3&&packets.info[at+0x6e]==2,"Loaded head is not a typed microvolt constant");
 const auto low=f32(packets.control,ct+4),high=f32(packets.control,ct+8),value=f32(packets.control,ct+0x10);
 require(std::isfinite(low)&&std::isfinite(high)&&std::isfinite(value)&&low==high&&high==value&&f32(packets.control,ct+0x14)==0&&f32(packets.control,ct+0x18)==0&&std::trunc(value)==value&&double(value)>=INT32_MIN&&double(value)<=INT32_MAX,"Constant head requires exact signed float32 microvolts and equal bounds/c0");
 return {static_cast<int64_t>(value),u16(packets.info,at+2),packets.info[at+0x6d]};
}
void same_clocks(const VbiosVoltageGraph& a,const VbiosVoltageGraph& b){require(a.clock_roots.size()==b.clock_roots.size(),"Scaffold changed a clock root");for(size_t i=0;i<a.clock_roots.size();++i){const auto& x=a.clock_roots[i];const auto& y=b.clock_roots[i];require(x.equation==y.equation&&x.entry==y.entry&&x.kind==y.kind&&x.offset==y.offset,"Scaffold changed a clock root");}}
}
InstalledVoltageScaffoldPlan plan_installed_voltage_scaffold(const Bytes& original,const VoltageEquationPackets& before,const Bytes& variables,const std::vector<std::pair<uint32_t,VbiosVoltageRole>>& selected){
 InstalledVoltageScaffoldPlan result;
 try{shape(before);require(variables.size()==0xc364,"Complete fresh target-bound variable INFO required");require(!selected.empty(),"Select at least one rail policy");
  auto graph=std::make_shared<VbiosVoltageGraph>(parse_vbios_voltage_graph(original));require(graph->ok&&graph->graph_complete&&graph->clock_roots_complete,"Original ROM ownership/sharing/clock graph unqualified");
  const auto rail=graph->rail_offset,vfe=graph->vfe_offset,base=graph->equation_base;const auto count=static_cast<uint32_t>(graph->equations.size());const uint32_t rails=original[rail+3];
  require(original[vfe+11]==5,"VFE term geometry unrecognized");const uint32_t end=base+25*count+5u*u16(original,vfe+12),rail_end=rail+5+65*rails;own(original,*graph,vfe,end-vfe,0x14);own(original,*graph,rail,rail_end-rail);
  std::set<std::pair<uint32_t,VbiosVoltageRole>> seen;for(const auto& request:selected){require(request.first<rails,"Unknown scaffold rail");(void)selector(request.second);require(seen.insert(request).second,"Duplicate scaffold policy selection");}
  for(uint32_t i=0;i<8192;++i)require(active(before.info,i)==(i<count&&graph->equations[i].kind!=0),"Loaded equation inventory differs from original image");
  std::set<uint32_t> possible;for(uint32_t i=0;i<rails;++i)for(const auto offset:{21u,23u}){const auto value=original[rail+5+65*i+offset];if(value!=255)possible.insert(value);}
  const std::array<uint8_t,25> empty={0,255,255,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,255};
  std::vector<uint32_t> slots;for(const auto& equation:graph->equations)if(equation.raw==empty&&!active(before.info,equation.index)&&equation.referenced_by.empty()&&!possible.count(equation.index)&&std::none_of(graph->clock_roots.begin(),graph->clock_roots.end(),[&](const auto& root){return root.equation==equation.index;}))slots.push_back(equation.index);
  require(slots.size()>=selected.size(),"Not enough canonical inactive equation slots");
  std::vector<uint32_t> order;std::set<uint32_t> ordered;
  for(const auto& request:selected){const uint32_t preferred=u16(original,rail+5+65*request.first+15);if(preferred<count&&ordered.insert(preferred).second)order.push_back(preferred);}
  for(uint32_t i=0;i<count;++i)if(ordered.insert(i).second)order.push_back(i);
  const VbiosVoltageEquation* pattern=nullptr;
  for(auto index:order){const auto& row=graph->equations[index];if(row.kind!=1||row.raw[23]!=2||row.variable>=255||!(variables[4+row.variable/8]&(1u<<(row.variable%8))))continue;
   if(!std::isfinite(row.lower)||!std::isfinite(row.upper)||!std::isfinite(row.coefficients[0])||!std::isfinite(row.coefficients[1])||!std::isfinite(row.coefficients[2])||row.lower!=row.upper||row.upper!=row.coefficients[0]||row.coefficients[1]!=0||row.coefficients[2]!=0)continue;
   try{const auto loaded=constant(before,index);if(double(row.lower)<INT32_MIN||double(row.lower)>INT32_MAX)continue;
    if(loaded.value==static_cast<int64_t>(row.lower)&&loaded.next==row.next&&loaded.variable==row.variable){pattern=&row;break;}}
   catch(const std::exception&){}
  }
  require(pattern!=nullptr,"No loaded constant template with a present variable");auto planned=std::make_shared<Bytes>(original);std::vector<InstalledVoltageScaffoldHead> heads;
  for(size_t i=0;i<selected.size();++i){const auto request=selected[i];const uint32_t row=rail+5+65*request.first,selection=row+selector(request.second),old=u16(original,selection);require(original[row]==1&&original[row+1]==request.first+1,"Scaffold rail identity unrecognized");
   require(old<count&&graph->equations[old].kind&&active(before.info,old)&&graph->equations[old].output==2,"Selected policy root unavailable or not microvolts");const auto& equation=graph->equations[old];const uint8_t type=equation.kind==1?3:equation.kind==2?2:equation.kind==3?1:equation.kind==7?6:0;
   const size_t pos=0x408+0x70*old;require(type&&before.info[pos]==type&&before.info[pos+0x6c]==type&&before.info[pos+0x6e]==2&&u16(before.info,pos+2)==equation.next,"Loaded selected policy root type/next differs from ROM");
   const uint32_t index=slots[i],at=base+25*index;(*planned)[at]=1;(*planned)[at+1]=pattern->variable;(*planned)[at+2]=uint8_t(old);
   std::fill_n(planned->begin()+at+3,12,uint8_t{0});(*planned)[at+23]=2;(*planned)[at+24]=uint8_t(old>>8);word(*planned,selection,index);
   heads.push_back({request.first,index,old,pattern->variable,2,selection,at,request.second,0});
  }
  const auto after=parse_vbios_voltage_graph(*planned);require(after.ok&&after.graph_complete&&after.clock_roots_complete,"Planned scaffold graph invalid");same_clocks(*graph,after);require(std::equal(original.begin()+base+25*count,original.begin()+end,planned->begin()+base+25*count),"Scaffold changed polynomial terms");
  auto state=std::make_shared<InstalledVoltageScaffoldPlan::State>();state->before=before;state->original=std::make_shared<const Bytes>(original);state->planned=planned;state->graph=graph;state->heads=heads;state->vfe_end=end;state->rail_end=rail_end;
  for(uint32_t i=0;i<original.size();++i)if(original[i]!=(*planned)[i])result.changes.push_back({i,original[i],(*planned)[i]});
  result.ok=true;result.message="Pure scaffold plan recognized; no installation authorized";result.heads=std::move(heads);result.original_image=state->original;result.planned_image=planned;result.original_graph=graph;result.state=std::move(state);
 }catch(const std::exception& error){result.message=error.what();}return result;
}
InstalledVoltageScaffoldResult recognize_installed_voltage_scaffold(const InstalledVoltageScaffoldPlan& plan,const Bytes& current,const VoltageEquationPackets& loaded,bool strict){
 InstalledVoltageScaffoldResult result;
 try{require(plan.ok&&plan.state,"Qualified scaffold plan required");const auto& state=*plan.state;shape(loaded);require(target_equal(state.before.target,loaded.target),"Loaded scaffold equation adapter changed");
  auto graph=std::make_shared<VbiosVoltageGraph>(parse_vbios_voltage_graph(current));require(graph->ok&&graph->graph_complete&&graph->clock_roots_complete,"Current scaffold ownership/graph unqualified");const auto& original=*state.graph;
  require(graph->vfe_offset==original.vfe_offset&&graph->rail_offset==original.rail_offset,"Scaffold table pointers changed");own(current,*graph,original.vfe_offset,state.vfe_end-original.vfe_offset,0x14);own(current,*graph,original.rail_offset,state.rail_end-original.rail_offset);same_clocks(original,*graph);
  for(const auto range:{std::pair<uint32_t,uint32_t>{original.vfe_offset,state.vfe_end},{original.rail_offset,state.rail_end}})require(std::equal(state.planned->begin()+range.first,state.planned->begin()+range.second,current.begin()+range.first),"Loaded VFE/rail scaffold differs from planned owned spans");
  Bytes expected_mask(state.before.info.begin()+4,state.before.info.begin()+0x404);for(const auto& head:state.heads){require(!active(state.before.info,head.index),"Scaffold reuses a previously active equation");expected_mask[head.index/8]|=uint8_t(1u<<(head.index%8));}
  require(std::equal(expected_mask.begin(),expected_mask.end(),loaded.info.begin()+4),"Loaded scaffold inventory differs from planned heads");
  for(const bool info:{true,false}){const auto& before=info?state.before.info:state.before.control;auto normalized=info?loaded.info:loaded.control;const uint32_t stride=info?0x70u:0x80u;
   std::copy_n(before.begin()+4,1024,normalized.begin()+4);for(const auto& head:state.heads){const auto at=0x408+head.index*stride;std::copy_n(before.begin()+at,stride,normalized.begin()+at);}require(normalized==before,"Pre-existing equation packet bytes changed");}
  auto heads=state.heads;for(auto& head:heads){const auto actual=constant(loaded,head.index);require(actual.next==head.next&&actual.variable==head.variable,"Installed constant head identity differs from plan");head.value_uv=actual.value;}
  result.complete_image_match=current==*state.planned;require(!strict||result.complete_image_match,"Activated image differs from complete planned image");result.ok=true;result.message="Installed constant heads recognized; policy/request evidence still required";result.heads=std::move(heads);result.current_graph=std::move(graph);
 }catch(const std::exception& error){result.message=error.what();}return result;
}
InstalledVoltageScaffoldResult recognize_installed_voltage_scaffold(const Bytes& original,const VoltageEquationPackets& before,const Bytes& variables,const std::vector<std::pair<uint32_t,VbiosVoltageRole>>& selected,const Bytes& current,const VoltageEquationPackets& loaded,bool strict){return recognize_installed_voltage_scaffold(plan_installed_voltage_scaffold(original,before,variables,selected),current,loaded,strict);}
}
