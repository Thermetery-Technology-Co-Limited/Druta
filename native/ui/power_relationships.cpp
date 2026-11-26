// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Pure fa6ee83 power_relationships.py/power_weight_runtime.py/vbios_fields.py
// port. ROM format research adapted there from Vix Editor (panchovix, GPL-3.0-or-later).
#include "power_relationships.h"
#include "vbios_rom_reader.h"
#include "backend/power_layout_catalog_generated.h"
#include <algorithm>
#include <cmath>
#include <set>
#include <stdexcept>
#include <regex>
#include <initializer_list>
#include <utility>
namespace druta::ui {
namespace {
using Bytes=std::vector<uint8_t>;
void require(bool condition,const char* message){if(!condition)throw std::runtime_error(message);}
uint32_t word32(const Bytes& b,size_t at){require(at<=b.size()&&4<=b.size()-at,"INFO extent missing");return uint32_t(b[at])|uint32_t(b[at+1])<<8|uint32_t(b[at+2])<<16|uint32_t(b[at+3])<<24;}
uint16_t word16(const Bytes& b,size_t at){require(at<=b.size()&&2<=b.size()-at,"INFO word extent missing");return uint16_t(b[at]|uint16_t(b[at+1])<<8);}
bool layout_equal(const PowerRelationshipLayout& a,const PowerRelationshipLayout& b) {
 return a.info_command==b.info_command&&a.info_size==b.info_size&&
        a.policy_base==b.policy_base&&a.policy_stride==b.policy_stride;
}

enum class InfoFormat { compact_rm26, rm26, ampere_a618, compact_a618, a618 };

constexpr bool is_rm26(InfoFormat format) {
 return format==InfoFormat::compact_rm26 || format==InfoFormat::rm26;
}

struct RelationshipInfoGeometry {
 InfoFormat format;
 PowerRelationshipLayout layout;
 size_t base;
 size_t weight_offset;

 size_t row(uint32_t index) const { return base + 0x24 * index; }
 size_t weight(uint32_t index) const { return row(index) + weight_offset; }
};

const RelationshipInfoGeometry* relationship_geometry(const PowerRelationshipLayout& layout) {
 static constexpr RelationshipInfoGeometry packets[] = {
  {InfoFormat::compact_rm26, {0x20802618,  5008, 0x34, 0x74},   0x0eb8, 4},
  {InfoFormat::rm26,         {0x20802618,  5392, 0x3c, 0x80},   0x1040, 4},
  {InfoFormat::rm26,         {0x20802618,  5520, 0x3c, 0x84},   0x10c0, 4},
  {InfoFormat::ampere_a618,  {0x2080a618,  7724, 0x58, 0xc8},   0x195c, 6},
  {InfoFormat::ampere_a618,  {0x2080a618,  8620, 0x58, 0xe4},   0x1cdc, 6},
  // The 8632-byte INFO keeps the relationship array before the appended masks.
  {InfoFormat::ampere_a618,  {0x2080a618,  8632, 0x58, 0xe4},   0x1cdc, 6},
  {InfoFormat::compact_a618, {0x2080a618, 20000, 0xcc, 0xfc},  0x3fd0, 6},
  {InfoFormat::a618,         {0x2080a618, 20512, 0xcc, 0x104}, 0x41d0, 6},
 };
 for (const auto& packet : packets)
  if (layout_equal(packet.layout, layout)) return &packet;
 return nullptr;
}

struct PolicyClass {
 uint8_t bios, runtime;
 bool editable = true;
};
using Classes = std::initializer_list<PolicyClass>;
constexpr Classes v30_policy_only = {{1,0}};
constexpr Classes v30_short = {{1,0},{4,3},{5,4},{8,9},{10,11}};
constexpr Classes v30_ampere = {{1,0},{4,3},{5,4},{16,15},{17,16}};
constexpr Classes v30_pascal = {{1,0},{4,3},{10,11}};
constexpr Classes v40_classes = {
 {1,0},{2,3},{3,4},{4,27,false},{5,28,false},{6,17},{7,18},{8,19,false},
 {9,20},{10,31,false},{11,30},{12,33},{13,32},{14,34,false},{15,36,false},{16,35,false}
};

constexpr uint32_t pascal = 4, turing = 6, ampere = 7;

struct RelationFormat {
 uint8_t stride_field, stride;
 std::optional<uint32_t> legacy_architecture;
 bool supports_a618;
 bool supports_ampere_a618 = false;

 bool supports(InfoFormat packet, uint32_t arch) const {
  if(packet==InfoFormat::ampere_a618) return supports_ampere_a618 && legacy_architecture==arch;
  if(packet==InfoFormat::compact_rm26 && arch!=turing) return false;
  return is_rm26(packet) ? legacy_architecture==arch : supports_a618;
 }
};
// The 0x42 rows keep their existing A618 support. Only Turing has qualified RM26.
constexpr RelationFormat short_relations{8,14,turing,true};
constexpr RelationFormat ampere_relations{8,14,ampere,false,true};
constexpr RelationFormat pascal_relations{8,10,pascal,false};
constexpr RelationFormat v40_relations{4,18,{},true};

struct PolicyFields {
 uint8_t class_mask;
 std::optional<uint8_t> class_extension;
 uint8_t unit_byte, unit_bit, channel, limits;
};
// The class extension at +0x42 belongs only to the longer V30 policy rows.
constexpr PolicyFields v30_fields{0x0f,{},0,4,1,2};
constexpr PolicyFields extended_v30_fields{0x0f,uint8_t{0x42},0,4,1,2};
constexpr PolicyFields v40_fields{0xff,{},1,0,5,13};

struct PowerTableFormat {
 uint8_t version, header_size, policy_stride;
 const PolicyFields& fields;
 Classes classes;
 const RelationFormat* relations;

 bool uses_objects() const { return version == 0x40; }
};
constexpr PowerTableFormat power_tables[] = {
 {0x30,0x32,0x53,extended_v30_fields,v30_policy_only,nullptr},
 {0x30,0x34,0x5b,extended_v30_fields,v30_policy_only,nullptr},
 {0x30,0x2c,0x4b,extended_v30_fields,v30_policy_only,nullptr},
 {0x30,0x2c,0x42,v30_fields,v30_short,&short_relations},
 {0x30,0x2c,0x47,extended_v30_fields,v30_ampere,&ampere_relations},
 {0x30,0x25,0x2e,v30_fields,v30_pascal,&pascal_relations},
 {0x40,0x38,0x66,v40_fields,v40_classes,&v40_relations},
 {0x40,0x3d,0x66,v40_fields,v40_classes,&v40_relations},
};
bool metadata_equal(const PowerRelationshipTarget& a,const PowerRelationshipTarget& b){return a.policy==b.policy&&a.type==b.type&&a.channel==b.channel&&a.unit==b.unit&&a.minimum==b.minimum&&a.default_value==b.default_value&&a.maximum==b.maximum;}
class Rom : private VbiosRomReader {
 const PowerTableFormat* format = nullptr;

 PowerWeightImagePolicy read_policy(uint32_t row, uint32_t index) const {
  const auto& fields = format->fields;
  uint32_t kind = byte(row) & fields.class_mask;
  if (fields.class_extension)
   kind |= uint32_t(byte(row + *fields.class_extension) & 15) << 4;

  PowerWeightImagePolicy policy;
  policy.index = index;
  policy.bios_type = kind;
  for (const auto& entry : format->classes) {
   if (entry.bios != kind) continue;
   const uint32_t unit = (byte(row + fields.unit_byte) >> fields.unit_bit) & 1;
   const auto limits = row + fields.limits;
   const auto minimum = u32(limits), rated = u32(limits + 4), maximum = u32(limits + 8);
   policy.metadata = PowerRelationshipTarget{index,entry.runtime,byte(row + fields.channel),
                                             unit,minimum,rated,maximum};
   policy.qualified = entry.editable && minimum <= rated && rated <= maximum;
   break;
  }
  if (!policy.qualified)
   policy.reason = "Unknown class translation or inconsistent/inspection-only policy limits";
  return policy;
 }

 void read_relationships(PowerWeightImage& out, const RelationFormat& relations) const {
  const auto field = out.table_offset + relations.stride_field;
  require(byte(field) == relations.stride, "Unknown relationship table geometry");
  const uint32_t count = byte(field + 1);
  own(out.table_offset, out.table_size + relations.stride * count, 0x13);
  out.relationship_count = count;
  for (uint32_t i = 0; i < std::min(count,32u); ++i) {
   const auto row = out.table_offset + out.table_size + relations.stride * i;
   PowerWeightImageRelationship r;
   r.index = i;
   r.offset = row;
   r.kind = byte(row) & 15;
   r.target = byte(row + 1);
   if (r.kind == 1) r.weight_raw = u16(row + 2);
   r.qualified = r.kind == 1 && r.target < std::min(out.count,32u) && out.policies[r.target].qualified;
   if (!r.qualified) r.reason = "Unknown relationship kind or unqualified target policy";
   out.relationships.push_back(std::move(r));
  }
  out.relationships_available = true;
 }

public:
 explicit Rom(const Bytes& image):VbiosRomReader(image){}

 bool supports_write(InfoFormat info, uint32_t architecture) const {
  return format && format->relations && format->relations->supports(info,architecture);
 }

 PowerWeightImage inspect(std::optional<uint32_t> expected_device_id){PowerWeightImage out;try{
  read_directory();
  if (expected_device_id)
   require(image_regions.front().device == *expected_device_id,"Firmware power table belongs to a different PCI device");
  out.selector_version = selector_version;
  out.selector_oem = selector_oem;
  const auto at = pointer('P',0x2c);
  const auto version = byte(at), header = byte(at+1), stride = byte(at+2), count = byte(at+3);
  for (const auto& candidate : power_tables) {
   if (candidate.version == version && candidate.header_size == header &&
       candidate.policy_stride == stride && candidate.uses_objects() == !object_regions.empty()) {
    format = &candidate;
    break;
   }
  }
  require(format && count, "Unrecognized power table geometry");
  const uint32_t size = header + stride * count;
  own(at,size,0x13);
  out.table_offset=at; out.table_version=version; out.header_size=header;
  out.stride=stride; out.count=count; out.table_size=size;
  for (uint32_t i=0; i<count; ++i)
   out.policies.push_back(read_policy(at + header + stride*i,i));

  if (format->relations) {
   try { read_relationships(out,*format->relations); }
   catch (const std::exception& error) { out.relationship_message=error.what(); }
  } else {
   out.relationship_message="Relationship-weight projection requires a supported relationship table";
  }
  out.ok=true;out.message="Power table inspected; runtime INFO identity/metadata qualification remains separate";
 }catch(const std::exception& error){out.message=error.what();}return out;}
};
}
bool power_relationship_layout_supported(const PowerRelationshipLayout& layout) {
 return relationship_geometry(layout) != nullptr;
}
bool power_policy_info_layout_supported(const PowerRelationshipLayout& layout) {
 for (const auto& candidate : druta::backend::catalog::kPowerLayouts) {
  const auto& packet=candidate.layout;
  const auto command=packet.has_commands ? packet.commands[0] : druta::backend::kPowerGetCommands[0];
  if (layout.info_command==command && layout.info_size==packet.sizes[0] &&
      layout.policy_base==packet.info_base && layout.policy_stride==packet.info_stride)
   return true;
 }
 return false;
}
PowerRelationshipReport decode_power_relationships(const PowerRelationshipLayout& layout,const Bytes& info){
 PowerRelationshipReport out;
 if(!power_policy_info_layout_supported(layout)){out.reason="Policy INFO geometry/command unrecognized";return out;}
 if(info.size()!=layout.info_size){out.reason="Policy INFO packet invalid/incomplete";return out;}
 const bool legacy_info=layout.info_command==0x20802618;
 out.policy_mask=word32(info,4);
 std::array<const char*,32> policy_errors{};
 for (uint32_t i=0; i<out.policies.size(); ++i) {
  if (!(out.policy_mask & (uint32_t{1} << i))) continue;
  const size_t row = layout.policy_base + layout.policy_stride*i;
  if (row>info.size() || 20>info.size()-row) {out.reason="Policy INFO packet invalid/incomplete";return out;}
  const auto version = info[row], type = info[row+4], channel = info[row+5], unit = info[row+6];
  if (version != 0x30 && (legacy_info || version != 0x40)) {
   policy_errors[i] = "Target policy row format unrecognized";
  } else if (unit > 1) {
   policy_errors[i] = "Target policy unit unrecognized";
  } else {
   out.policies[i] = PowerRelationshipTarget{i,type,channel,unit,word32(info,row+8),word32(info,row+12),word32(info,row+16)};
  }
 }
 out.policies_available=true;
 const auto geometry=relationship_geometry(layout);
 if (!geometry) {out.reason="Policy definitions decoded. Relationship rows have no known layout in this packet.";return out;}
 const bool compact_info=geometry->format==InfoFormat::compact_a618;
 out.relationship_mask=word32(info,geometry->base-4);
 for(uint32_t i=0;i<32;++i)if(out.relationship_mask&(1u<<i)){const size_t at=geometry->row(i);PowerRelationshipRecord r;r.index=i;r.type=info[at];r.target=info[at+1];if(!r.type)r.weight_raw=word16(info,geometry->weight(i));
  if(r.target>=32||!(out.policy_mask&(1u<<r.target)))r.target_reason="Target policy absent from INFO mask";
  else if (policy_errors[r.target]) r.target_reason=policy_errors[r.target];
  else r.target_metadata=out.policies[r.target];
  out.relationships.push_back(std::move(r));
 }
 const auto relation=[&](uint32_t i)->const PowerRelationshipRecord*{for(const auto& r:out.relationships)if(r.index==i)return &r;return nullptr;};
 const auto fill=[&](PowerRelationshipSource& source){for(uint32_t i=source.first;i<=source.last;++i){const auto* r=relation(i);if(!r||r->type||!r->target_metadata){source.reason="Referenced relationship or target unavailable";source.relationships.clear();return;}source.relationships.push_back(i);}source.available=true;};
 for(uint32_t i=0;i<32;++i)if(out.policy_mask&(1u<<i)){const size_t at=layout.policy_base+layout.policy_stride*i;const auto kind=info[at+4];const bool format=info[at]==0x30||info[at]==0x40;
  // These layouts expose the relationship rows. Policy source-field offsets
  // still require separate qualification on their shorter records.
  if(legacy_info||geometry->format==InfoFormat::ampere_a618){out.source_uninterpreted_policies.push_back(i);continue;}
  if(!compact_info&&kind==33){PowerRelationshipSource source;source.policy=i;source.first=word16(info,at+0x34);source.last=word16(info,at+0x36);
   if(!format)source.reason="Source policy row format unrecognized";else if(source.first>source.last)source.available=true;else if(source.last>=32)source.reason="Source relationship range exceeds INFO capacity";else fill(source);out.range_references.push_back(std::move(source));continue;}
  if(!compact_info&&kind==32){PowerRelationshipLink source;source.policy=i;source.relationship=word16(info,at+0x34);const auto* r=relation(source.relationship);if(r)source.target=r->target;
   if(!format)source.reason="Source policy row format unrecognized";else if(source.relationship>=32||!r)source.reason="Linked relationship absent from INFO mask";else if(r->type||!r->target_metadata)source.reason="Linked relationship or target unavailable";else source.available=true;out.linked_references.push_back(std::move(source));continue;}
  if(kind){out.source_uninterpreted_policies.push_back(i);continue;}
  PowerRelationshipSource source;source.policy=i;
  const size_t first=compact_info?0xb4:0xbc;
  source.first=word16(info,at+first);source.last=word16(info,at+first+2);
  const uint8_t a=info[at+(compact_info?0xb1:0xb9)],b=compact_info?0:info[at+0xc0];
  if(!format)source.reason="Source policy row format unrecognized";else if(a>1||b>1)source.reason="Source relationship flags unrecognized";
  else{source.enabled=a||b;if(!*source.enabled)source.available=true;else if(source.first>source.last||source.last>=32)source.reason="Source relationship range invalid";else fill(source);}out.source_references.push_back(std::move(source));
 }
 out.available=true;return out;
}
PowerWeightImage inspect_power_weight_image(const Bytes& image,std::optional<uint32_t> expected_device_id){
 return Rom(image).inspect(expected_device_id);
}
const char* firmware_power_policy_evidence(const PowerWeightImagePolicy& policy,const PowerRelationshipReport& live) {
 if (!policy.metadata) return "Unknown BIOS policy class";
 const auto& bios = *policy.metadata;
 if (bios.minimum > bios.default_value || bios.default_value > bios.maximum) return "Inconsistent BIOS bounds";
 if (!live.policies_available) return "BIOS only";
 if (policy.index >= live.policies.size() || !(live.policy_mask & (uint32_t{1} << policy.index))) return "Absent from driver";
 const auto& row = live.policies[policy.index];
 if (!row) return "Unknown driver row";
 if (row->type != bios.type || row->unit != bios.unit || row->channel != bios.channel) return "Driver binding differs";
 return metadata_equal(*row,bios) ? "Matches driver definition" : "Driver bounds differ";
}
uint16_t power_weight_raw(double multiplier){const auto raw=multiplier*4096;require(std::isfinite(multiplier)&&std::isfinite(raw)&&raw>=0&&raw<=65535&&std::trunc(raw)==raw,"Weight must be exact unsigned Q4.12");return static_cast<uint16_t>(raw);}
struct PowerWeightPlan::State {PowerRelationshipIdentity identity;PowerRelationshipLayout layout;std::vector<PowerWeightSelection> selected;std::shared_ptr<const Bytes> image,after_image,info,after_info;};
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
 PowerWeightPlan out;try{
  identity_valid(identity);
  require(!requests.empty() && requests.size() <= 32, "Select relationship weights");
  Rom rom(image);
  const auto inspection = rom.inspect(identity.device_id);
  require(inspection.ok,inspection.message.c_str());
  require(inspection.ok && inspection.relationships_available, "Unique supported image relationship table required");
  const auto geometry = relationship_geometry(layout);
  const auto report = decode_power_relationships(layout,info);
  require(geometry && report.available, "Validated relationship INFO required");
  require(rom.supports_write(geometry->format,identity.architecture),"Relationship write layout is unqualified for this architecture and INFO geometry");
  auto state=std::make_shared<PowerWeightPlan::State>();state->identity=identity;state->layout=layout;auto next_image=std::make_shared<Bytes>(image),next_info=std::make_shared<Bytes>(info);std::set<uint32_t> seen;
  for (const auto& request : requests) {
   require(request.index < 32 && seen.insert(request.index).second, "Duplicate or out-of-capacity relationship selection");
   const auto wanted = power_weight_raw(request.multiplier);
   const auto r = std::find_if(inspection.relationships.begin(),inspection.relationships.end(),
                               [&](const auto& row){ return row.index == request.index; });
   require(r != inspection.relationships.end() && r->qualified && r->kind == 1 && r->weight_raw,
           "Selected image relationship or target unqualified");
   const auto& policy = inspection.policies[r->target];
   require(policy.qualified && policy.metadata, "Selected image target metadata unqualified");
  if (geometry->format == InfoFormat::ampere_a618) {
   const auto target = layout.policy_base + layout.policy_stride * r->target;
   require(info[target] == 0x30, "Selected Ampere INFO target requires a V30 policy row");
  }
   const auto before = *r->weight_raw;
   if (request.expected_raw) require(*request.expected_raw == before, "Selected expected image weight changed");

   PowerWeightSelection selected{r->index,r->offset,r->target,before,wanted,*policy.metadata};
   checked(report,selected,before);
   state->selected.push_back(selected);
   put16(*next_image,r->offset+2,wanted);
   put16(*next_info,geometry->weight(r->index),wanted);
   if (wanted != before)
    out.patches.push_back({r->offset+2,{uint8_t(before),uint8_t(before>>8)},{uint8_t(wanted),uint8_t(wanted>>8)}});
  }
  state->image=std::make_shared<const Bytes>(image);state->info=std::make_shared<const Bytes>(info);state->after_image=next_image;state->after_info=next_info;out.identity=identity;out.selected=state->selected;out.original_image=state->image;out.expected_image=next_image;out.original_info=state->info;out.expected_info=next_info;out.state=std::move(state);out.ok=true;out.message="Selected exact Q4.12 spans projected; no activation authorized";
 }catch(const std::exception& error){out.message=error.what();}return out;
}
PowerWeightVerification verify_power_relationship_weights(const PowerWeightPlan& plan,const PowerRelationshipIdentity& identity,const PowerRelationshipLayout& layout,const Bytes& info,const Bytes* image,bool original){PowerWeightVerification result;try{
 require(plan.ok&&plan.state,"Qualified relationship plan required");identity_valid(identity);const auto& state=*plan.state;require(identity_equal(identity,state.identity),"Relationship adapter identity changed");require(layout_equal(layout,state.layout),"Relationship INFO layout changed");const auto report=decode_power_relationships(layout,info);require(report.available,"Current relationship INFO geometry invalid");require(info==*(original?state.info:state.after_info),"Selected weights or unselected INFO bytes differ");if(image)require(*image==*(original?state.image:state.after_image),"Complete relationship image differs");for(const auto& selected:state.selected)checked(report,selected,original?selected.before_raw:selected.after_raw);result.ok=true;result.image_verified=image!=nullptr;result.message=image?"Exact selected weights, preserved INFO and complete image verified":"Exact selected weights and preserved INFO verified; image activation not checked";
 }catch(const std::exception& error){result.message=error.what();}return result;}
}
