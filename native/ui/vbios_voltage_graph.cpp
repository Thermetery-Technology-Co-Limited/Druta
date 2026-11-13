// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Read-only format port: fa6ee83 src/druta/vbios_fields.py. Original format
// research/adapted rules: NVIDIA VBIOS Vix Editor, Copyright (C) 2026 panchovix.
#include "vbios_voltage_graph.h"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>
#include <map>
#include <set>
#include <stdexcept>
namespace druta::ui {
namespace {
void require(bool condition,const char* message){if(!condition)throw std::runtime_error(message);}
struct Token {uint8_t version;uint16_t size,offset;};
class Parser {
 const std::vector<uint8_t>& b;
 VbiosVoltageGraph result;
 std::map<uint8_t,Token> tokens;
 VbiosImageRegion legacy;std::optional<VbiosImageRegion> efi;
 void span(size_t at,size_t size)const{require(at<=b.size()&&size<=b.size()-at,"Capture does not contain selected ROM extent");}
 uint8_t byte(size_t at)const{span(at,1);return b[at];}
 uint16_t u16(size_t at)const{span(at,2);return uint16_t(b[at]|uint16_t(b[at+1])<<8);}
 uint32_t u32(size_t at)const{span(at,4);return uint32_t(b[at])|uint32_t(b[at+1])<<8|uint32_t(b[at+2])<<16|uint32_t(b[at+3])<<24;}
 float f32(size_t at)const{const auto raw=u32(at);float value=0;std::memcpy(&value,&raw,4);return value;}
 bool magic(size_t at,const char* value,size_t n)const{return at<=b.size()&&n<=b.size()-at&&std::memcmp(b.data()+at,value,n)==0;}
 static size_t end(const VbiosImageRegion& image){return size_t(image.start)+image.size;}
 void images(){
  for(size_t start=0;start+26<=b.size();start+=512){
   const bool pci=magic(start,"\x55\xaa",2),npds=magic(start,"VN",2);if(!pci&&!npds)continue;
   const size_t at=start+u16(start+24);if(!magic(at,pci?"PCIR":"NPDS",4))continue;
   span(at,24);const auto size=uint32_t(u16(at+16))*512u;const auto header=u16(at+10);
   require(at>=start+26&&header>=24&&size>0&&at+header<=start+size,"Invalid PCI/NPDS header geometry");span(at,header);
   result.images.push_back({static_cast<uint32_t>(start),size,static_cast<uint32_t>(at),u16(at+4),u16(at+6),byte(at+20),start+size<=b.size(),npds});
  }
  for(size_t i=1;i<result.images.size();++i)require(end(result.images[i-1])<=result.images[i].start,"Overlapping PCI/NPDS images");
  unsigned found=0;for(const auto& image:result.images)if(image.type==0&&image.vendor==0x10de){legacy=image;++found;}
  require(found==1&&legacy.start==0&&legacy.complete,"Expected one complete NVIDIA legacy image at offset zero");
  for(const auto& image:result.images)if(image.start==end(legacy)&&image.type==3)efi=image;
  if(efi)require(efi->complete&&((efi->vendor==0&&efi->device==0)||(efi->vendor==0x10de&&(efi->device==0||efi->device==legacy.device))),"Inconsistent or truncated EFI image");
 }
 void bit(){
  unsigned found=0;for(size_t at=0;at+6<=end(legacy);++at){if(!magic(at,"\xff\xb8" "BIT\0",6))continue;
   span(at,12);const auto hs=byte(at+8),stride=byte(at+9),count=byte(at+10);
   require(hs>=12&&stride>=6&&count&&at+hs+size_t(stride)*count<=end(legacy),"Invalid BIT dimensions");
   unsigned checksum=0;span(at,hs);for(size_t k=at;k<at+hs;++k)checksum+=byte(k);require((checksum&255)==0,"BIT header checksum mismatch");
   std::map<uint8_t,Token> candidate;for(unsigned i=0;i<count;++i){const auto row=at+hs+size_t(stride)*i;const auto id=byte(row);
    Token token{byte(row+1),u16(row+2),u16(row+4)};require(!candidate.count(id)&&size_t(token.offset)+token.size<=end(legacy),"Duplicate or out-of-bounds BIT token");candidate.emplace(id,token);}
   tokens=std::move(candidate);++found;
  }
  require(found==1,"Expected one BIT directory");const auto i=tokens.find('i');require(i!=tokens.end()&&i->second.version==2&&i->second.size>=5,"No understood BIT i version selector");
  result.selector_version=u32(i->second.offset);result.selector_oem=byte(size_t(i->second.offset)+4);
 }
 void objects(){
  if(!efi)return;std::optional<VbiosImageRegion> meta;for(const auto& image:result.images)if(image.start==end(*efi)&&image.type==0xe0&&!image.npds){require(!meta,"Inconsistent metadata image");meta=image;}
  if(!meta)return;require(meta->vendor==0x10de,"Inconsistent metadata image");size_t archive_end=0;for(const auto& image:result.images)archive_end=std::max(archive_end,end(image));
  unsigned candidates=0;
  for(size_t at=meta->start;at+8<=meta->header;++at){if(u32(at)!=3||u32(at+4)!=16)continue;
   span(at,16);const auto count=u32(at+8);if(count<2||count>1024||u32(at+12)!=legacy.device)continue;
   const auto base=at+16+60u*count;if(base>meta->header)continue;
   std::vector<VbiosObjectRegion> entries;for(uint32_t i=0;i<count;++i){const auto row=at+16+60u*i;const auto size=u32(row+8);const auto data=base+u32(row+4);
    if(!size||size>vbios_voltage_max_image_bytes||data+size>archive_end)break;
    entries.push_back({u32(row),static_cast<uint32_t>(data),size,static_cast<uint32_t>(row+12),data+size<=b.size()});}
   if(entries.size()!=count||entries[0].id!=0xfb||entries[1].id!=0xbe||entries[1].offset!=meta->header)continue;
   std::set<uint32_t> ids;for(const auto& entry:entries)require(ids.insert(entry.id).second,"Duplicate metadata object IDs");
   result.objects=std::move(entries);++candidates;
  }
  require(candidates==1,"No unique bounded v3 metadata object directory");
 }
 uint32_t pointer(uint8_t token,uint32_t offset)const{
  const auto it=tokens.find(token);require(it!=tokens.end()&&it->second.version==2&&offset+4<=it->second.size,"Absent or unrecognized BIT table pointer");
  size_t ptr=u32(size_t(it->second.offset)+offset);require(ptr!=0,"Unpopulated BIT table pointer");if(ptr>=legacy.size&&efi)ptr+=efi->size;
  span(ptr,4);require(!efi||ptr<efi->start||ptr>=end(*efi),"Table pointer addresses EFI code");return static_cast<uint32_t>(ptr);
 }
 void own(size_t at,size_t size,std::optional<uint32_t> expected={})const{
  span(at,size);unsigned owners=0;uint32_t id=0;
  if(!result.objects.empty()&&at>=end(legacy)){
   for(const auto& object:result.objects)if(object.offset<=at&&at+size<=size_t(object.offset)+object.size){++owners;id=object.id;}
   require(owners==1&&(!expected||id==*expected),"Table lacks its unique expected object owner");return;
  }
  for(const auto& image:result.images)if(image.type!=3&&image.start<=at&&at+size<=end(image))++owners;
  require(owners==1,"Table crosses image ownership boundary");
 }
 void equations(){
  result.rail_offset=pointer('P',0x68);result.vfe_offset=pointer('P',0x20);
  const auto rail=result.rail_offset,vfe=result.vfe_offset;
  require(byte(rail)==0x30&&byte(rail+1)==5&&byte(rail+2)==65&&(byte(rail+3)==1||byte(rail+3)==2),"Unrecognized rail selector layout");own(rail,5+65u*byte(rail+3));
  require(byte(vfe)==0x30&&byte(vfe+1)==14&&byte(vfe+2)==29&&byte(vfe+3)==255&&byte(vfe+4)==25,"Unrecognized voltage equation layout");
  const auto count=uint32_t(byte(vfe+5))|uint32_t(byte(vfe+10))<<8;require(count&&count<=4096,"Voltage equation count outside understood layout");
  result.equation_base=vfe+14+29*255;own(vfe,size_t(result.equation_base)+25u*count-vfe,0x14);
  for(uint32_t i=0;i<255;++i){std::array<uint8_t,29> row{};std::copy_n(b.begin()+vfe+14+29u*i,29,row.begin());result.variable_rows.push_back(row);}
  for(uint32_t i=0;i<count;++i){VbiosVoltageEquation e;e.index=i;e.offset=result.equation_base+25*i;std::copy_n(b.begin()+e.offset,25,e.raw.begin());
   e.kind=e.raw[0];e.variable=e.raw[1];e.next=uint16_t(e.raw[2]|uint16_t(e.raw[24])<<8);e.output=e.raw[23]&15;
   e.lower=f32(e.offset+3);e.upper=f32(e.offset+7);for(unsigned j=0;j<3;++j)e.coefficients[j]=f32(e.offset+11+4*j);result.equations.push_back(e);}
 }
 void graph(){
  const auto vfe=result.vfe_offset,base=result.equation_base;const auto count=static_cast<uint32_t>(result.equations.size());uint32_t term_count=0;
  if(std::any_of(result.equations.begin(),result.equations.end(),[](const auto& e){return e.kind==7;})){
   require(byte(vfe+11)==5,"Unrecognized VFE polynomial term stride");term_count=u16(vfe+12);require(term_count>0,"VFE polynomial term table absent");own(vfe,size_t(base)+25u*count+5u*term_count-vfe,0x14);}
  for(auto& e:result.equations){require(e.kind==0||e.kind==1||e.kind==2||e.kind==3||e.kind==4||e.kind==7,"Unrecognized VFE equation kind in sharing graph");if(!e.kind)continue;
   auto add=[&](uint32_t target){if(target==0xffff)return;require(target<count&&result.equations[target].kind,"VFE link does not identify an active equation");e.edges.push_back(target);};add(e.next);
   if(e.kind==2||e.kind==3){const unsigned a=e.kind==2?11u:15u;add(e.raw[a]|uint32_t(e.raw[a+4])<<8);add(e.raw[a+1]|uint32_t(e.raw[a+5])<<8);}
   if(e.kind==7){const uint32_t first=u16(e.offset+11),variables=e.raw[13]&15;const uint32_t coefficients=variables==1?3u:variables==2?6u:variables==3?10u:0u;
    require(coefficients&&first+variables+coefficients<=term_count,"VFE polynomial terms exceed understood geometry");
    for(uint32_t j=0;j<variables+coefficients;++j){VbiosVoltageTerm term;term.index=first+j;const auto at=base+25u*count+5u*term.index;std::copy_n(b.begin()+at,5,term.raw.begin());term.kind=byte(at);
     if(term.kind==1){term.value=u32(at+1);term.literal=f32(at+1);require(std::isfinite(*term.literal),"Nonfinite VFE polynomial literal");}
     else if(term.kind==2){term.value=byte(at+1);require(term.value<byte(vfe+3),"VFE polynomial variable exceeds its table");}
     else if(term.kind==3){term.value=u16(at+1);require(term.value!=0xffff,"VFE polynomial equation term absent");add(term.value);}
     else require(false,"Unrecognized active VFE polynomial term kind");
     (j<variables?e.variables:e.terms).push_back(term);
    }
   }
  }
  std::vector<uint32_t> indegree(count);for(const auto& e:result.equations)for(auto target:e.edges)++indegree[target];
  std::vector<uint32_t> ready;for(uint32_t i=0;i<count;++i)if(!indegree[i])ready.push_back(i);uint32_t visited=0;
  while(!ready.empty()){const auto i=ready.back();ready.pop_back();++visited;for(auto target:result.equations[i].edges)if(--indegree[target]==0)ready.push_back(target);}
  require(visited==count,"Cyclic VFE sharing graph");
  for(const auto& e:result.equations)for(auto target:e.edges)result.equations[target].referenced_by.push_back(e.index);
  for(auto& e:result.equations){auto& refs=e.referenced_by;std::sort(refs.begin(),refs.end());refs.erase(std::unique(refs.begin(),refs.end()),refs.end());}
  result.graph_complete=true;
 }
 void clock_roots(){
  const auto it=tokens.find('C');if(it==tokens.end()){result.clock_roots_complete=true;return;}
  require(it->second.version==2&&it->second.size>=0x10,"External sharing: unrecognized BIT C pointer layout");
  if(!u32(size_t(it->second.offset)+0x0c)){result.clock_roots_complete=true;return;}
  const auto at=pointer('C',0x0c);span(at,8);const auto version=byte(at),hs=byte(at+1),size=byte(at+2),rows=byte(at+3);
  require(hs==8&&((version==0x40&&size==15)||(version==0x50&&size==23)),"External sharing: unrecognized clock programming geometry");
  const auto c1=byte(at+4),s1=byte(at+5),c2=byte(at+6),s2=byte(at+7);require((!c1||s1)&&(!c2||s2),"External sharing: invalid clock subentry geometry");
  const uint32_t stride=size+uint32_t(c1)*s1+uint32_t(c2)*s2;own(at,hs+size_t(stride)*rows,4);
  std::vector<VbiosVoltageClockRoot> roots;for(uint32_t i=0;i<rows;++i){const auto row=at+hs+stride*i;const uint32_t kind=byte(row)&15;
   const uint32_t idx=version==0x50?u16(row+3):uint32_t(byte(row+3))|uint32_t(byte(row+13))<<8;
   if(kind==0&&idx==0xffff)continue;require(kind==1||kind==3,"External sharing: unrecognized active clock programming kind");if(idx==0xffff)continue;
   require(idx<result.equations.size()&&result.equations[idx].kind,"External sharing: clock root is not an active VFE equation");roots.push_back({idx,i,kind,row+3});}
  result.clock_roots=std::move(roots);result.clock_roots_complete=true;
 }
 void rail_roots(){
  constexpr uint32_t offsets[]={13,15,17,19,33,35};constexpr VbiosVoltageRole names[]={VbiosVoltageRole::rel,VbiosVoltageRole::alt,VbiosVoltageRole::ov,VbiosVoltageRole::vmin,VbiosVoltageRole::info_vmin,VbiosVoltageRole::unnamed};
  const auto count=byte(result.rail_offset+3);for(uint32_t i=0;i<count;++i){const auto row=result.rail_offset+5+65*i;require(byte(row)==1&&byte(row+1)==i+1,"Unrecognized rail identity");
   for(unsigned j=0;j<6;++j){const uint32_t idx=u16(row+offsets[j]);if(idx==0xffff)continue;
    if(idx>=result.equations.size()||!result.equations[idx].kind){result.graph_complete=false;result.graph_error="Rail selector does not identify an active VFE equation";}
    result.rail_roots.push_back({i,byte(row+1),row+offsets[j],idx,names[j]});}
   for(const auto offset:{21u,23u}){const uint32_t idx=byte(row+offset);if(idx!=255&&idx<result.equations.size()&&result.equations[idx].kind)result.rail_roots.push_back({i,byte(row+1),row+offset,idx,VbiosVoltageRole::possible_byte});}
  }
 }
 std::vector<uint32_t> closure(uint32_t root,bool reverse)const{
  std::vector<bool> seen(result.equations.size());std::vector<uint32_t> todo{root};while(!todo.empty()){const auto i=todo.back();todo.pop_back();if(seen[i])continue;seen[i]=true;
   const auto& adjacent=reverse?result.equations[i].referenced_by:result.equations[i].edges;todo.insert(todo.end(),adjacent.begin(),adjacent.end());}
  std::vector<uint32_t> out;for(uint32_t i=0;i<seen.size();++i)if(seen[i])out.push_back(i);return out;
 }
 using Range=std::optional<std::array<double,2>>;
 std::vector<Range> ranges()const{
  const auto count=result.equations.size();std::vector<Range> values(count);if(!result.graph_complete)return values;
  std::vector<uint32_t> remaining(count),depth(count);std::vector<uint32_t> ready;
  for(const auto& e:result.equations){std::set<uint32_t> distinct(e.edges.begin(),e.edges.end());remaining[e.index]=static_cast<uint32_t>(distinct.size());if(distinct.empty())ready.push_back(e.index);}
  const auto get=[&](uint32_t i)->Range{return i==0xffff?Range{std::array<double,2>{0,0}}:values[i];};
  while(!ready.empty()){const auto index=ready.back();ready.pop_back();const auto& e=result.equations[index];
   bool valid=e.output==2&&std::isfinite(e.lower)&&std::isfinite(e.upper)&&e.lower<=e.upper;double low=e.lower,high=e.upper;
   for(auto next:e.edges)depth[index]=std::max(depth[index],depth[next]+1);
   if(depth[index]>512)valid=false;
   if(e.kind==1||e.kind==7){if(e.kind==1&&e.coefficients[1]==0&&e.coefficients[2]==0&&std::isfinite(e.coefficients[0]))low=high=e.coefficients[0];}
   else if(e.kind==2||e.kind==3){const unsigned a=e.kind==2?11u:15u;auto left=get(e.raw[a]|uint32_t(e.raw[a+4])<<8),right=get(e.raw[a+1]|uint32_t(e.raw[a+5])<<8);
    if(!left||!right)valid=false;else{low=std::min((*left)[0],(*right)[0]);high=std::max((*left)[1],(*right)[1]);}}
   else valid=false;
   if(valid){low=std::max(double(e.lower),std::min(double(e.upper),low));high=std::min(double(e.upper),std::max(double(e.lower),high));const auto tail=get(e.next);
    if(tail&&std::isfinite(low)&&std::isfinite(high)&&std::abs(low)<1e30&&std::abs(high)<1e30)values[index]=std::array<double,2>{low+(*tail)[0],high+(*tail)[1]};}
   for(auto user:e.referenced_by)if(--remaining[user]==0)ready.push_back(user);
  }
  return values;
 }
 static std::optional<int64_t> whole(float value){const double d=value;if(!std::isfinite(d)||d!=std::trunc(d)||d<INT32_MIN||d>INT32_MAX)return {};return static_cast<int64_t>(d);}
 void roles(){
  if(result.graph_complete)for(auto& e:result.equations){e.dependencies=closure(e.index,false);e.affected=closure(e.index,true);}
  const auto range=ranges();
  for(const auto& root:result.rail_roots){if(root.role==VbiosVoltageRole::unnamed||root.role==VbiosVoltageRole::possible_byte)continue;
   require(root.equation<result.equations.size(),"Rail selector exceeds voltage equations");const auto& e=result.equations[root.equation];VbiosVoltageRoleBinding role;role.rail=root.rail;role.role=root.role;role.equation=root.equation;
   role.graph_complete=result.graph_complete&&result.clock_roots_complete;
   role.known=(e.kind==1||(role.role==VbiosVoltageRole::rel&&e.kind==7))&&e.output==2&&std::isfinite(e.lower)&&std::isfinite(e.upper)&&e.lower>=0&&e.lower<=e.upper;
   role.stored_lower=whole(e.lower);role.stored_value=whole(e.upper);
   const auto aliases=std::count_if(result.rail_roots.begin(),result.rail_roots.end(),[&](const auto& r){return r.equation==root.equation;});
   role.shared=!role.graph_complete||!e.referenced_by.empty()||std::any_of(result.clock_roots.begin(),result.clock_roots.end(),[&](const auto& r){return r.equation==root.equation;})||aliases!=1;
   uint32_t rel=0xffff;for(const auto& r:result.rail_roots)if(r.rail==root.rail&&r.role==VbiosVoltageRole::rel)rel=r.equation;
   if(rel==0xffff)role.rel_range=std::array<double,2>{0,0};else if(rel<range.size())role.rel_range=range[rel];
   if(role.graph_complete){role.dependencies=e.dependencies;role.affected=e.affected;
    for(const auto& r:result.rail_roots)if(std::binary_search(e.affected.begin(),e.affected.end(),r.equation))role.affected_rail_roots.push_back(r);
    for(const auto& r:result.clock_roots)if(std::binary_search(e.affected.begin(),e.affected.end(),r.equation))role.affected_clock_roots.push_back(r);}
   const bool constant=role.known&&e.kind==1&&e.lower==e.upper&&e.upper==e.coefficients[0]&&e.coefficients[1]==0&&e.coefficients[2]==0;
   if(role.role==VbiosVoltageRole::rel)role.encoding_qualified=role.known&&e.kind==7&&role.graph_complete&&role.stored_lower&&role.stored_value&&*role.stored_value>0;
   else if(role.role!=VbiosVoltageRole::info_vmin){const bool structure=role.role==VbiosVoltageRole::alt?(e.next==rel&&role.rel_range.has_value()):e.next==0xffff;
    role.encoding_qualified=constant&&!role.shared&&structure&&role.stored_value&&*role.stored_value>=(role.role==VbiosVoltageRole::alt?0:1)&&
       (role.role!=VbiosVoltageRole::vmin||role.rel_range.has_value());}
   result.roles.push_back(std::move(role));
  }
 }
public:
 explicit Parser(const std::vector<uint8_t>& image):b(image){}
 VbiosVoltageGraph parse(){try{
  require(b.size()>=0x1a&&b.size()<=vbios_voltage_max_image_bytes,"VBIOS size outside bounded parser");images();bit();objects();equations();
  try{graph();}catch(const std::exception& error){result.graph_complete=false;result.graph_error=error.what();}
  try{clock_roots();}catch(const std::exception& error){result.clock_roots_complete=false;result.clock_error=error.what();}
  rail_roots();roles();result.ok=true;result.message=result.graph_complete&&result.clock_roots_complete?"Bounded voltage graph inspected; runtime qualification still required":"Voltage definitions inspected; sharing graph incomplete";
 }catch(const std::exception& error){result.ok=false;result.graph_complete=false;result.clock_roots_complete=false;for(auto& role:result.roles){role.encoding_qualified=false;role.graph_complete=false;}result.message=error.what();}
 return std::move(result);}
};
}
VbiosVoltageGraph parse_vbios_voltage_graph(const std::vector<uint8_t>& image){return Parser(image).parse();}
}
