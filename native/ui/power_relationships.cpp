// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Pure fa6ee83 power_relationships.py/power_weight_runtime.py/vbios_fields.py
// port. ROM format research adapted there from Vix Editor (panchovix, GPL-3.0-or-later).
#include "power_relationships.h"
#include "vbios_voltage_graph.h"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <map>
#include <set>
#include <stdexcept>
#include <cstdio>
#include <regex>
#include <utility>
namespace druta::ui {
namespace {
using Bytes=std::vector<uint8_t>;
void require(bool condition,const char* message){if(!condition)throw std::runtime_error(message);}
uint32_t word32(const Bytes& b,size_t at){require(at<=b.size()&&4<=b.size()-at,"INFO extent missing");return uint32_t(b[at])|uint32_t(b[at+1])<<8|uint32_t(b[at+2])<<16|uint32_t(b[at+3])<<24;}
uint16_t word16(const Bytes& b,size_t at){require(at<=b.size()&&2<=b.size()-at,"INFO word extent missing");return uint16_t(b[at]|uint16_t(b[at+1])<<8);}
bool layout_valid(const PowerRelationshipLayout& l){return l.info_command==0x2080a618&&l.info_size==20512&&l.policy_base==0xcc&&l.policy_stride==0x104;}
bool metadata_equal(const PowerRelationshipTarget& a,const PowerRelationshipTarget& b){return a.policy==b.policy&&a.type==b.type&&a.channel==b.channel&&a.unit==b.unit&&a.minimum==b.minimum&&a.default_value==b.default_value&&a.maximum==b.maximum;}
std::optional<uint32_t> runtime_type(uint32_t version,uint32_t kind){if(version==0x30)return kind==1?std::optional<uint32_t>{0}:std::nullopt;static constexpr uint32_t types[]={0,0,3,4,27,28,17,18,19,20,31,30,33,32,34,36,35};if(version==0x40&&kind>=1&&kind<=16)return types[kind];return {};}
bool editable_class(uint32_t kind){return kind==1||kind==2||kind==3||kind==6||kind==7||kind==9||kind==11||kind==12||kind==13;}
struct Token {uint8_t version;uint16_t size,offset;};
class Rom {
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
public:
 explicit Rom(const Bytes& image):b(image){}
 PowerWeightImage inspect(){PowerWeightImage out;try{
  require(b.size()>=0x1a&&b.size()<=vbios_voltage_max_image_bytes,"VBIOS size outside bounded parser");images();bit();objects();out.selector_version=result.selector_version;out.selector_oem=result.selector_oem;
  const auto at=pointer('P',0x2c);const uint32_t version=byte(at),hs=byte(at+1),stride=byte(at+2),count=byte(at+3);
  const bool modern=!result.objects.empty();const bool known=modern?(version==0x40&&(hs==0x38||hs==0x3d)&&stride==0x66):(version==0x30&&((hs==0x32&&stride==0x53)||(hs==0x34&&stride==0x5b)||(hs==0x2c&&(stride==0x4b||stride==0x47))));
  require(known&&count,"Unrecognized power table geometry");const uint32_t size=hs+stride*count;own(at,size,0x13);
  out.table_offset=at;out.table_version=version;out.header_size=hs;out.stride=stride;out.count=count;out.table_size=size;
  for(uint32_t i=0;i<count;++i){const auto row=at+hs+stride*i;const uint32_t kind=modern?byte(row):(byte(row)&15)|uint32_t(byte(row+0x42)&15)<<4;PowerWeightImagePolicy policy;policy.index=i;policy.bios_type=kind;
   const auto translated=runtime_type(version,kind);const auto unit=modern?(u32(row+1)&1):uint32_t(byte(row)>>4)&1;const auto base=row+(modern?13u:2u);const auto lo=u32(base),def=u32(base+4),hi=u32(base+8);
   if(kind&&translated)policy.metadata=PowerRelationshipTarget{i,*translated,byte(row+(modern?5u:1u)),unit,lo,def,hi};
   policy.qualified=kind&&translated&&editable_class(kind)&&lo<=def&&def<=hi&&!(version==0x30&&stride==0x47&&kind!=1);
   if(!policy.qualified)policy.reason="Unknown class translation or inconsistent/inspection-only policy limits";out.policies.push_back(std::move(policy));
  }
  if(modern){try{const uint32_t relation_stride=byte(at+4),relations=byte(at+5);require(relation_stride==18,"Unknown relationship table geometry");own(at,size+relation_stride*relations,0x13);out.relationship_count=relations;
    for(uint32_t i=0;i<std::min(relations,32u);++i){const auto row=at+size+18*i;PowerWeightImageRelationship r;r.index=i;r.offset=row;r.kind=byte(row)&15;r.target=byte(row+1);r.weight_raw=u16(row+2);
     r.qualified=r.kind==1&&r.target<std::min(count,32u)&&out.policies[r.target].qualified;
     if(!r.qualified)r.reason="Unknown relationship kind or unqualified target policy";out.relationships.push_back(std::move(r));}
    out.relationships_available=true;
   }catch(const std::exception& error){out.relationship_message=error.what();}}
  else out.relationship_message="Relationship-weight projection requires the understood V40 object-owned layout";
  out.ok=true;out.message="Power table inspected; runtime INFO identity/metadata qualification remains separate";
 }catch(const std::exception& error){out.message=error.what();}return out;}
};
}
PowerRelationshipReport decode_power_relationships(const PowerRelationshipLayout& layout,const Bytes& info){
 PowerRelationshipReport out;if(!layout_valid(layout)){out.reason="Policy INFO geometry/command unrecognized";return out;}if(info.size()!=20512){out.reason="Policy INFO packet invalid/incomplete";return out;}
 out.policy_mask=word32(info,4);out.relationship_mask=word32(info,0x41cc);
 for(uint32_t i=0;i<32;++i)if(out.relationship_mask&(1u<<i)){const size_t at=0x41d0+0x24*i;PowerRelationshipRecord r;r.index=i;r.type=info[at];r.target=info[at+1];if(!r.type)r.weight_raw=word16(info,at+6);
  if(r.target>=32||!(out.policy_mask&(1u<<r.target)))r.target_reason="Target policy absent from INFO mask";
  else{const size_t row=0xcc+0x104*r.target;const auto version=word32(info,row)&255,header=word32(info,row+4),unit=(header>>16)&255;
   if(version!=0x30&&version!=0x40)r.target_reason="Target policy row format unrecognized";else if(unit>1)r.target_reason="Target policy unit unrecognized";
   else r.target_metadata=PowerRelationshipTarget{r.target,header&255,(header>>8)&255,unit,word32(info,row+8),word32(info,row+12),word32(info,row+16)};}
  out.relationships.push_back(std::move(r));
 }
 const auto relation=[&](uint32_t i)->const PowerRelationshipRecord*{for(const auto& r:out.relationships)if(r.index==i)return &r;return nullptr;};
 const auto fill=[&](PowerRelationshipSource& source){for(uint32_t i=source.first;i<=source.last;++i){const auto* r=relation(i);if(!r||r->type||!r->target_metadata){source.reason="Referenced relationship or target unavailable";source.relationships.clear();return;}source.relationships.push_back(i);}source.available=true;};
 for(uint32_t i=0;i<32;++i)if(out.policy_mask&(1u<<i)){const size_t at=0xcc+0x104*i;const auto kind=info[at+4];const bool format=info[at]==0x30||info[at]==0x40;
  if(kind==33){PowerRelationshipSource source;source.policy=i;source.first=word16(info,at+0x34);source.last=word16(info,at+0x36);
   if(!format)source.reason="Source policy row format unrecognized";else if(source.first>source.last)source.available=true;else if(source.last>=32)source.reason="Source relationship range exceeds INFO capacity";else fill(source);out.range_references.push_back(std::move(source));continue;}
  if(kind==32){PowerRelationshipLink source;source.policy=i;source.relationship=word16(info,at+0x34);const auto* r=relation(source.relationship);if(r)source.target=r->target;
   if(!format)source.reason="Source policy row format unrecognized";else if(source.relationship>=32||!r)source.reason="Linked relationship absent from INFO mask";else if(r->type||!r->target_metadata)source.reason="Linked relationship or target unavailable";else source.available=true;out.linked_references.push_back(std::move(source));continue;}
  if(kind){out.source_uninterpreted_policies.push_back(i);continue;}
  PowerRelationshipSource source;source.policy=i;source.first=word16(info,at+0xbc);source.last=word16(info,at+0xbe);const auto a=info[at+0xb9],b=info[at+0xc0];
  if(!format)source.reason="Source policy row format unrecognized";else if(a>1||b>1)source.reason="Source relationship flags unrecognized";
  else{source.enabled=a||b;if(!*source.enabled)source.available=true;else if(source.first>source.last||source.last>=32)source.reason="Source relationship range invalid";else fill(source);}out.source_references.push_back(std::move(source));
 }
 out.available=true;return out;
}
PowerWeightImage inspect_power_weight_image(const Bytes& image){return Rom(image).inspect();}
uint16_t power_weight_raw(double multiplier){const auto raw=multiplier*4096;require(std::isfinite(multiplier)&&std::isfinite(raw)&&raw>=0&&raw<=65535&&std::trunc(raw)==raw,"Weight must be exact unsigned Q4.12");return static_cast<uint16_t>(raw);}
struct PowerWeightPlan::State {PowerRelationshipIdentity identity;std::vector<PowerWeightSelection> selected;std::shared_ptr<const Bytes> image,after_image,info,after_info;};
namespace {
void identity_valid(const PowerRelationshipIdentity& id){
 static const std::regex slot("^([0-9A-Fa-f]{4}|[0-9A-Fa-f]{8}):[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}\\.[0-7]$");
 require(std::regex_match(id.slot,slot),"Invalid paired relationship PCI identity");
 const auto colon=id.slot.find(':');
 require(std::stoul(id.slot.substr(0,colon),nullptr,16)==0&&std::stoul(id.slot.substr(colon+4,2),nullptr,16)<=31,"Invalid paired relationship PCI identity");
 require(id.architecture>=4&&id.architecture<=10&&id.device_id<=0xffff,"Relationship architecture/identity outside released probe contract");}
void put16(Bytes& bytes,size_t at,uint16_t value){require(at<=bytes.size()&&2<=bytes.size()-at,"Weight projection outside bytes");bytes[at]=uint8_t(value);bytes[at+1]=uint8_t(value>>8);}
const PowerRelationshipRecord& checked(const PowerRelationshipReport& report,const PowerWeightSelection& selected,uint16_t raw){const auto found=std::find_if(report.relationships.begin(),report.relationships.end(),[&](const auto& r){return r.index==selected.index;});require(found!=report.relationships.end()&&found->type==0&&found->target==selected.target&&found->weight_raw==raw&&found->target_metadata&&metadata_equal(*found->target_metadata,selected.metadata),"INFO relationship weight/target metadata differs from current image binding");return *found;}
bool identity_equal(const PowerRelationshipIdentity& a,const PowerRelationshipIdentity& b){return a.slot==b.slot&&a.architecture==b.architecture&&a.device_id==b.device_id&&a.subsystem_id==b.subsystem_id;}
}
PowerWeightPlan plan_power_relationship_weights(const Bytes& image,const PowerRelationshipIdentity& identity,const PowerRelationshipLayout& layout,const Bytes& info,const std::vector<PowerWeightRequest>& requests){
 PowerWeightPlan out;try{identity_valid(identity);require(!requests.empty()&&requests.size()<=32,"Select relationship weights");const auto inspection=inspect_power_weight_image(image);require(inspection.ok&&inspection.relationships_available&&inspection.table_version==0x40&&(inspection.header_size==0x38||inspection.header_size==0x3d)&&inspection.stride==0x66,"Unique understood V40 image relationship table required");const auto report=decode_power_relationships(layout,info);require(report.available,"Validated relationship INFO required");
  auto state=std::make_shared<PowerWeightPlan::State>();state->identity=identity;auto next_image=std::make_shared<Bytes>(image),next_info=std::make_shared<Bytes>(info);std::set<uint32_t> seen;
  for(const auto& request:requests){require(request.index<32&&seen.insert(request.index).second,"Duplicate or out-of-capacity relationship selection");const auto wanted=power_weight_raw(request.multiplier);const auto r=std::find_if(inspection.relationships.begin(),inspection.relationships.end(),[&](const auto& row){return row.index==request.index;});require(r!=inspection.relationships.end()&&r->qualified&&r->kind==1,"Selected image relationship or target unqualified");const auto& policy=inspection.policies[r->target];require(policy.qualified&&policy.metadata,"Selected image target metadata unqualified");if(request.expected_raw)require(*request.expected_raw==r->weight_raw,"Selected expected image weight changed");
   PowerWeightSelection selected{r->index,r->offset,r->target,r->weight_raw,wanted,*policy.metadata};checked(report,selected,selected.before_raw);state->selected.push_back(selected);
   put16(*next_image,r->offset+2,wanted);put16(*next_info,0x41d0+0x24*r->index+6,wanted);
   if(wanted!=r->weight_raw)out.patches.push_back({r->offset+2,{uint8_t(r->weight_raw),uint8_t(r->weight_raw>>8)},{uint8_t(wanted),uint8_t(wanted>>8)}});
  }
  state->image=std::make_shared<const Bytes>(image);state->info=std::make_shared<const Bytes>(info);state->after_image=next_image;state->after_info=next_info;out.identity=identity;out.selected=state->selected;out.original_image=state->image;out.expected_image=next_image;out.original_info=state->info;out.expected_info=next_info;out.state=std::move(state);out.ok=true;out.message="Selected exact Q4.12 spans projected; no activation authorized";
 }catch(const std::exception& error){out.message=error.what();}return out;
}
PowerWeightVerification verify_power_relationship_weights(const PowerWeightPlan& plan,const PowerRelationshipIdentity& identity,const PowerRelationshipLayout& layout,const Bytes& info,const Bytes* image,bool original){PowerWeightVerification result;try{
 require(plan.ok&&plan.state,"Qualified relationship plan required");identity_valid(identity);const auto& state=*plan.state;require(identity_equal(identity,state.identity),"Relationship adapter identity changed");const auto report=decode_power_relationships(layout,info);require(report.available,"Current relationship INFO geometry invalid");require(info==*(original?state.info:state.after_info),"Selected weights or unselected INFO bytes differ");if(image)require(*image==*(original?state.image:state.after_image),"Complete relationship image differs");for(const auto& selected:state.selected)checked(report,selected,original?selected.before_raw:selected.after_raw);result.ok=true;result.image_verified=image!=nullptr;result.message=image?"Exact selected weights, preserved INFO and complete image verified":"Exact selected weights and preserved INFO verified; image activation not checked";
 }catch(const std::exception& error){result.message=error.what();}return result;}
}
