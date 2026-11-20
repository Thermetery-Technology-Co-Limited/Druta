// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_relationships.h"
#define main unused_power_relationship_rom_fixture_main
#include "vbios_voltage_graph_test.cpp"
#undef main
#include <limits>
#include <set>
namespace {
struct PowerCase {std::vector<uint8_t> image,info;PowerRelationshipIdentity identity{"0000:39:00.0",7,0x99aa,0x12345678};};
PowerCase power_case(uint8_t header=0x38){PowerCase c;c.image=fixture(true,0x99aa);constexpr uint32_t table=object_vfe;
 dword(c.image,0x22c,table-0x1000);dword(c.image,0x2210+120,0x13);std::fill_n(c.image.begin()+table,header+32*0x66+32*18,uint8_t{0});
 c.image[table]=0x40;c.image[table+1]=header;c.image[table+2]=0x66;c.image[table+3]=32;c.image[table+4]=18;c.image[table+5]=32;
 const auto policy=[&](uint32_t index,uint8_t kind,uint32_t flags,uint8_t channel,uint32_t lo,uint32_t def,uint32_t hi){const auto at=table+header+0x66*index;c.image[at]=kind;dword(c.image,at+1,flags);c.image[at+5]=channel;dword(c.image,at+13,lo);dword(c.image,at+17,def);dword(c.image,at+21,hi);};
 policy(9,11,0xa5a50001,9,1,45000,45000);policy(2,1,0xf0000000,3,5,100,300);
 const auto base=table+header+32*0x66;c.image[base]=0x81;c.image[base+1]=9;word(c.image,base+2,4096);c.image[base+31*18]=1;c.image[base+31*18+1]=2;word(c.image,base+31*18+2,6144);
 c.info.resize(20512);dword(c.info,4,(1u<<9)|(1u<<2)|(1u<<0)|(1u<<1)|(1u<<3));dword(c.info,0x41cc,(1u<<0)|(1u<<31));
 const auto row=[&](uint32_t index,uint32_t format,uint32_t type,uint32_t channel,uint32_t unit,uint32_t lo,uint32_t def,uint32_t hi){const auto at=0xcc+0x104*index;dword(c.info,at,format);dword(c.info,at+4,type|(channel<<8)|(unit<<16));dword(c.info,at+8,lo);dword(c.info,at+12,def);dword(c.info,at+16,hi);};
 row(9,0x40,30,9,1,1,45000,45000);row(2,0x30,0,3,0,5,100,300);row(0,0x40,0,0,0,1,2,3);row(1,0x30,32,0,0,1,2,3);row(3,0x40,33,0,0,1,2,3);
 c.info[0x41d0+1]=9;word(c.info,0x41d0+6,4096);c.info[0x41d0+31*0x24+1]=2;word(c.info,0x41d0+31*0x24+6,6144);
 c.info[0xcc+0xb9]=1;word(c.info,0xcc+0xbc,0);word(c.info,0xcc+0xbe,0);word(c.info,0xcc+0x104+0x34,31);word(c.info,0xcc+3*0x104+0x34,31);word(c.info,0xcc+3*0x104+0x36,31);c.info[0x5000]=0xab;return c;}
void short_v30_tests() {
 const PowerRelationshipLayout layouts[]={{0x2080a618,20000,0xcc,0xfc},{0x20802618,5520,0x3c,0x84},{0x2080a618,20512,0xcc,0x104}};
 const PowerRelationshipIdentity identity{"0000:7b:00.0",5,0x5678,0xbeef0001};
 // Put the table near the end of its owner so a bogus relationship count
 // cannot be mistaken for another table or data beyond the image.
 constexpr size_t table=0x5e00,first=table+0x2c,second=first+0x42,relation=second+0x42;
 auto rom=fixture(false,0x5678);
 dword(rom,0x22c,table);
 const uint8_t header[]={0x30,0x2c,0x42,2,0,0,0,0,14,2};
 std::copy(std::begin(header),std::end(header),rom.begin()+table);
 rom[first]=1;rom[first+1]=9;
 dword(rom,first+2,100000);dword(rom,first+6,260000);dword(rom,first+10,320000);
 rom[second]=0x15;rom[second+1]=7;
 dword(rom,second+2,20000);dword(rom,second+6,350780);dword(rom,second+10,390000);
 rom[relation]=1;rom[relation+1]=1;word(rom,relation+2,4096);
 rom[relation+14]=2;rom[relation+15]=1;word(rom,relation+16,0xbeef);

 const auto parsed=inspect_power_weight_image(rom);
 check(parsed.ok&&parsed.relationships_available&&parsed.policies.size()==2&&parsed.relationships.size()==2,
       "Read both short V30 policies and their following relationship records");
 if(!parsed.ok||!parsed.relationships_available)return;
 check(parsed.policies[0].bios_type==1&&parsed.policies[1].bios_type==5&&parsed.relationships[0].qualified,
       "The next record's class bits do not alter the current policy");
 check(parsed.relationships[1].kind==2 && !parsed.relationships[1].weight_raw && !parsed.relationships[1].qualified,
       "Opaque firmware relationship payloads have no decoded weight");
 for(const auto& layout:layouts) {
  const bool legacy=layout.info_command==0x20802618;
  const size_t target_info=layout.policy_base+layout.policy_stride;
  const bool compact=layout.info_size==20000;
  const size_t relation_info=legacy?0x10c0:(compact?0x3fd0:0x41d0);
  const size_t weight_offset=legacy?4:6;
  std::vector<uint8_t> info(layout.info_size);
  dword(info,4,3);
  dword(info,relation_info-4,3);
  info[layout.policy_base]=0x30;
  if(!legacy)info[layout.policy_base+(compact?0xb1:0xb9)]=1;
  info[target_info]=0x30;info[target_info+4]=4;info[target_info+5]=7;info[target_info+6]=1;
  dword(info,target_info+8,20000);dword(info,target_info+12,350780);dword(info,target_info+16,390000);
  info[relation_info+1]=1;word(info,relation_info+weight_offset,4096);
  info[relation_info+0x24]=1;info[relation_info+0x24+1]=1;

  const auto report=decode_power_relationships(layout,info);
  check(report.available&&report.relationships.size()==2,"Each qualified INFO layout decodes the short V30 weight and target");
  if(!legacy)check(report.source_references.size()==1&&
        report.source_references.front().relationships==std::vector<uint32_t>{0},"A618 INFO also decodes the source range");
  const std::vector<PowerWeightRequest> requests={{0,4097.0/4096,4096}};
  const auto plan=plan_power_relationship_weights(rom,identity,layout,info,requests);
  check(plan.ok&&plan.patches.size()==1&&plan.patches.front().offset==relation+2,
        "Matching V30 weight and target qualify for override Preview");
  auto turing=identity;
  turing.architecture=6;
  check(plan_power_relationship_weights(rom,turing,layout,info,requests).ok,
        "Turing 0x42 rows qualify with matching A618 and RM26 packets");
  if(plan.ok) {
   auto expected_rom=rom,expected_info=info;
   word(expected_rom,relation+2,4097);word(expected_info,relation_info+weight_offset,4097);
   check(*plan.expected_image==expected_rom&&*plan.expected_info==expected_info&&
         verify_power_relationship_weights(plan,identity,layout,expected_info,&expected_rom).ok&&
         verify_power_relationship_weights(plan,identity,layout,info,&rom,true).ok,
         "Apply and Restore project only the selected V30 image and INFO weight word");
   expected_info.back()^=1;
   check(!verify_power_relationship_weights(plan,identity,layout,expected_info,&expected_rom).ok,
         "Unselected bytes remain part of the independent activation proof");
  }
  check(!plan_power_relationship_weights(rom,identity,layout,info,{{1,1,{}}}).ok,
        "Opaque relation kind is inspectable but cannot produce a patch");
  info[target_info+5]^=1;
  check(!plan_power_relationship_weights(rom,identity,layout,info,requests).ok,
        "A mismatched target channel disqualifies the weight");
 }
 rom[table+9]=255;
 const auto oversized=inspect_power_weight_image(rom);
 check(oversized.ok&&!oversized.relationships_available,"Out-of-image V30 relationship extent is rejected");
}
void legacy_info_tests() {
 const PowerRelationshipLayout layout{0x20802618,5520,0x3c,0x84};
 constexpr size_t base=0x10c0, stride=0x24;
 const uint32_t policy_mask=(1u<<0)|(1u<<4)|(1u<<31);
 const uint32_t relationship_mask=(1u<<1)|(1u<<5)|(1u<<13)|(1u<<31);
 std::vector<uint8_t> info(layout.info_size);
 dword(info,4,policy_mask);
 dword(info,base-4,relationship_mask);
 const auto last_policy=layout.policy_base+31*layout.policy_stride;
 info[layout.policy_base]=0x30;
 info[last_policy]=0x30;
 dword(info,last_policy+4,11|(9u<<8)|(1u<<16));
 dword(info,last_policy+8,1000);
 dword(info,last_policy+12,123456);
 dword(info,last_policy+16,150000);
 const auto row=base+stride;
 info[row+1]=31;
 word(info,row+4,5301);
 word(info,row+6,1); // Distinct bytes at the newer layouts' weight offset.
 info[base+5*stride]=1;
 info[base+5*stride+1]=31;
 word(info,base+5*stride+4,7);
 info[base+13*stride+1]=24; // Absent from the mask.
 info[base+31*stride+1]=4; // Present, with an unrecognized row format.
 word(info,base+31*stride+4,65535);
 info.back()=0xa5;

 const auto report=decode_power_relationships(layout,info);
 check(report.available&&report.relationships.size()==4&&report.policy_mask==policy_mask&&
       report.relationship_mask==relationship_mask,"Legacy INFO honors sparse masks through the last supported index");
 if(!report.available||report.relationships.size()!=4)return;
 const auto& first=report.relationships.front();
 check(first.index==1&&first.target==31&&first.weight_raw==5301&&first.target_metadata&&
       first.target_metadata->type==11&&first.target_metadata->channel==9&&first.target_metadata->unit==1&&
       first.target_metadata->minimum==1000&&first.target_metadata->default_value==123456&&
       first.target_metadata->maximum==150000,"Legacy weight offset and target policy metadata decoded");
 check(report.relationships[1].type==1&&!report.relationships[1].weight_raw&&report.relationships[1].target_metadata&&
       !report.relationships[2].target_metadata&&report.relationships[3].index==31&&
       report.relationships[3].weight_raw==65535&&!report.relationships[3].target_metadata,
       "Unknown, absent and invalid records stay independent of valid weights");
 check(report.source_references.empty()&&report.linked_references.empty()&&report.range_references.empty()&&
       report.source_uninterpreted_policies==std::vector<uint32_t>{0,4,31},"Legacy source fields remain opaque");
 auto truncated=info;
 truncated.pop_back();
 check(!decode_power_relationships(layout,truncated).available,"Legacy INFO rejects incomplete packets");
 for(const auto invalid:{PowerRelationshipLayout{0x2080a618,5520,0x3c,0x84},
                         PowerRelationshipLayout{0x20802618,5520,0xcc,0x84},
                         PowerRelationshipLayout{0x20802618,5520,0x3c,0x88}})
  check(!decode_power_relationships(invalid,info).available,"Legacy INFO requires the exact command and policy geometry");
 const auto mismatched=power_case();
 const auto plan=plan_power_relationship_weights(mismatched.image,mismatched.identity,layout,info,{{1,1.25,5301}});
 check(!plan.ok&&plan.patches.empty()&&plan.message.find("write layout is unqualified")!=std::string::npos,
       "Legacy INFO refuses a V40 ROM even when its relationship table is valid");
}
// A sparse packet with the same selected metadata on a different, valid wire layout.
std::vector<uint8_t> single_target_packet(const PowerRelationshipLayout& layout, size_t relation_base,
                                           uint32_t index, uint8_t target, const uint8_t* policy,
                                           uint16_t weight) {
 std::vector<uint8_t> packet(layout.info_size);
 dword(packet,4,1u<<target);
 dword(packet,relation_base-4,1u<<index);
 std::copy_n(policy,20,packet.begin()+layout.policy_base+layout.policy_stride*target);
 const auto row=relation_base+0x24*index;
 packet[row+1]=target;
 word(packet,row+(layout.info_command==0x20802618?4:6),weight);
 return packet;
}

void rm26_tests() {
 const PowerRelationshipLayout layout{0x20802618,5520,0x3c,0x84};
 struct PolicyClass { uint8_t bios, runtime; };
 constexpr size_t table=0x5e00, relation_info=0x10c0, selected=5;
 for (const bool ampere : {false,true}) {
  const PowerRelationshipIdentity identity{"0000:29:00.0",ampere?7u:4u,0x4321,0x8a2b0023};
  const uint8_t header_size=ampere?0x2c:0x25, policy_stride=ampere?0x47:0x2e;
  const uint8_t relation_stride=ampere?14:10;
  const std::vector<PolicyClass> classes=ampere
      ? std::vector<PolicyClass>{{1,0},{4,3},{5,4},{16,15},{17,16}}
      : std::vector<PolicyClass>{{1,0},{4,3},{10,11}};
  const auto policy_count=static_cast<uint8_t>(classes.size());
  const auto relation_start=table+header_size+policy_stride*policy_count;
  const auto selected_rom=relation_start+relation_stride*selected;
  const auto selected_info=relation_info+0x24*selected;
  auto rom=fixture(false,0x4321);
  dword(rom,0x22c,table);
  const uint8_t header[]={0x30,header_size,policy_stride,policy_count,0,0,0,0,relation_stride,6};
  std::copy(std::begin(header),std::end(header),rom.begin()+table);
  std::vector<uint8_t> info(layout.info_size);
  dword(info,4,(1u<<policy_count)-1);
  dword(info,relation_info-4,0x21);
  for (uint32_t index=0;index<policy_count;++index) {
   const auto at=table+header_size+policy_stride*index;
   const auto row=layout.policy_base+layout.policy_stride*index;
   const uint8_t channel=static_cast<uint8_t>(6+index);
   const uint8_t unit=static_cast<uint8_t>(index%2);
   const uint32_t minimum=1000+index, rated=50000+index, maximum=70000+index;
   rom[at]=static_cast<uint8_t>((classes[index].bios&15)|(unit<<4));
   rom[at+1]=channel;
   if(ampere)rom[at+0x42]=classes[index].bios>>4;
   dword(rom,at+2,minimum);dword(rom,at+6,rated);dword(rom,at+10,maximum);
   info[row]=0x30;info[row+4]=classes[index].runtime;info[row+5]=channel;info[row+6]=unit;
   dword(info,row+8,minimum);dword(info,row+12,rated);dword(info,row+16,maximum);
  }
  rom[relation_start]=1;rom[relation_start+1]=1;word(rom,relation_start+2,6144);
  info[relation_info+1]=1;word(info,relation_info+4,6144);
  rom[selected_rom]=1;rom[selected_rom+1]=policy_count-1;word(rom,selected_rom+2,4096);
  info[selected_info+1]=policy_count-1;word(info,selected_info+4,4096);
  word(info,selected_info+6,0xbeef);
  rom[selected_rom+4]=0xa7;
  info.back()=0x5a;

  const auto image=inspect_power_weight_image(rom);
  const auto report=decode_power_relationships(layout,info);
  check(image.ok&&image.relationships_available&&image.relationships.size()==6&&
        image.relationships[selected].qualified,
        "Installed V30 layouts decode their own sparse relationship rows");
  if(image.ok&&image.relationships_available) {
   for(uint32_t index=0;index<policy_count;++index) {
    const auto& policy=image.policies[index];
    check(policy.bios_type==classes[index].bios&&policy.metadata&&
          policy.metadata->type==classes[index].runtime,"V30 class translation follows ROM policy geometry");
   }
  }
  check(report.available&&report.relationships.size()==2&&report.relationships.back().index==selected&&
        report.relationships.back().weight_raw==4096&&report.source_references.empty()&&
        report.range_references.empty()&&report.linked_references.empty()&&
        report.source_uninterpreted_policies.size()==policy_count,
        "RM26 weights use +4; unqualified source fields remain uninterpreted");
  const std::vector<PowerWeightRequest> requests={{selected,1.25,4096}};
  const auto plan=plan_power_relationship_weights(rom,identity,layout,info,requests);
  check(plan.ok&&plan.patches.size()==1&&plan.patches.front().offset==selected_rom+2,
        "Validated RM26 and the matching V30 firmware project one weight");
  if(plan.ok) {
   auto expected_rom=rom, expected_info=info;
   word(expected_rom,selected_rom+2,5120);word(expected_info,selected_info+4,5120);
   check(*plan.expected_image==expected_rom&&*plan.expected_info==expected_info&&
         verify_power_relationship_weights(plan,identity,layout,expected_info,&expected_rom).ok&&
         verify_power_relationship_weights(plan,identity,layout,info,&rom,true).ok,
         "Activation and removal check exact full RM26 INFO and V30 image bytes");
   expected_info.back()^=1;
   check(!verify_power_relationship_weights(plan,identity,layout,expected_info).ok,
         "An unselected RM26 byte invalidates proof");
   PowerRelationshipLayout alternate_wire=layout;
   alternate_wire.info_command=0x2080a618;
   check(!verify_power_relationship_weights(plan,identity,alternate_wire,*plan.expected_info).ok,
         "A changed INFO routing command invalidates proof");
  }
  auto wrong_identity=identity;
  wrong_identity.architecture=ampere?4:7;
  check(!plan_power_relationship_weights(rom,wrong_identity,layout,info,requests).ok,
        "The exact architecture and ROM/INFO combination is required for a write");
  const auto target=layout.policy_base+layout.policy_stride*(policy_count-1);
  for (const auto& a618 : {PowerRelationshipLayout{},PowerRelationshipLayout{0x2080a618,20000,0xcc,0xfc}}) {
   const auto packet=single_target_packet(a618,a618.info_size==20000?0x3fd0:0x41d0,selected,
                                          policy_count-1,info.data()+target,4096);
   const auto decoded=decode_power_relationships(a618,packet);
   check(decoded.available && decoded.relationships.size()==1 && decoded.relationships[0].target_metadata &&
         decoded.relationships[0].weight_raw==4096,"Alternate A618 wire fixture decodes independently");
   const auto mismatch=plan_power_relationship_weights(rom,identity,a618,packet,requests);
   check(!mismatch.ok && mismatch.patches.empty() && mismatch.message.find("write layout is unqualified")!=std::string::npos,
         "Matching weight and target metadata cannot qualify an A618 packet for RM26-only firmware");
  }
  auto wrong_info=info;
  wrong_info[target]=0x40;
  check(!plan_power_relationship_weights(rom,identity,layout,wrong_info,requests).ok,
        "A non-V30 RM26 target row cannot qualify a weight");
  // Change one decoded field at a time, keeping the other target metadata valid.
  for (const auto field : {4,5,6,8,12,16}) {
   wrong_info=info;
   wrong_info[target+field]^=1;
   check(!plan_power_relationship_weights(rom,identity,layout,wrong_info,requests).ok,
         "RM26 requires agreement on type, channel, unit and each of the three limits");
  }
  auto wrong_rom=rom;
  const auto target_rom=table+header_size+policy_stride*(policy_count-1);
  wrong_rom[target_rom]^=2;
  check(!plan_power_relationship_weights(wrong_rom,identity,layout,info,requests).ok,
        "Unknown V30 classes remain outside the typed relationship contract");
  wrong_rom=rom;
  ++wrong_rom[table+8];
  check(!inspect_power_weight_image(wrong_rom).relationships_available,
        "The exact V30 relationship row stride is required");
 }
}
void read_only_table_tests() {
 for (const auto& shape : {std::pair<uint8_t,uint8_t>{0x32,0x53},{0x34,0x5b},{0x2c,0x4b}}) {
  constexpr size_t table=0x400;
  auto rom=fixture();
  dword(rom,0x22c,table);
  const auto [header,stride]=shape;
  rom[table]=0x30; rom[table+1]=header; rom[table+2]=stride; rom[table+3]=2;
  rom[table+8]=14; rom[table+9]=1;
  rom[table+header]=1;
  rom[table+header+stride]=4; // Known on some V30 tables only.
  const auto inspection=inspect_power_weight_image(rom);
  check(inspection.ok && inspection.policies.size()==2 && inspection.policies[0].metadata &&
        !inspection.policies[1].metadata && !inspection.relationships_available,
        "Legacy policy-only tables retain their own translations and expose no weight rows");
 }
 auto c=power_case();
 constexpr size_t target=object_vfe+0x38+9*0x66;
 c.image[target]=4;
 c.info[0xcc+9*0x104+4]=27;
 const auto inspection=inspect_power_weight_image(c.image);
 check(inspection.relationships_available && inspection.policies[9].metadata &&
       inspection.policies[9].metadata->type==27 && !inspection.relationships[0].qualified &&
       !plan_power_relationship_weights(c.image,c.identity,{},c.info,{{0,1.25,4096}}).ok,
       "A translated inspection-only V40 class stays read-only even with matching INFO");
}

void modern_pairing_tests() {
 const auto c=power_case();
 for (const auto& layout : {PowerRelationshipLayout{0x2080a618,20000,0xcc,0xfc},
                            PowerRelationshipLayout{0x20802618,5520,0x3c,0x84}}) {
  const bool compact=layout.info_size==20000;
  const size_t base=compact?0x3fd0:0x10c0;
  auto packet=single_target_packet(layout,base,0,9,c.info.data()+0xcc+9*0x104,4096);
  packet[layout.policy_base+9*layout.policy_stride]=0x30; // Understood on both wires.
  const auto decoded=decode_power_relationships(layout,packet);
  check(decoded.available && decoded.relationships.size()==1 && decoded.relationships[0].target_metadata,
        "V40 pairing fixture has an independently valid target");
  const auto plan=plan_power_relationship_weights(c.image,c.identity,layout,packet,{{0,1.25,4096}});
  check(plan.ok==compact,"V40 weights qualify on compact A618 and refuse matched RM26");
 }
 auto rom=c.image;
 rom[0x10b]^=1;
 check(!inspect_power_weight_image(rom).ok,"Power inspection enforces the BIT checksum");
 rom=c.image;
 dword(rom,0x2210+120,0x14);
 check(!inspect_power_weight_image(rom).ok,"Power inspection requires the power object as owner");
 rom=c.image;
 word(rom,0x1086,0xabcd);
 check(!inspect_power_weight_image(rom).ok,"Power inspection validates the adjacent EFI identity");
}

void power_tests(){PowerRelationshipLayout layout;
 for(const auto header:{uint8_t{0x38},uint8_t{0x3d}}){auto c=power_case(header);const auto original=c.image,info=c.info;const auto image=inspect_power_weight_image(c.image);const auto report=decode_power_relationships(layout,c.info);
  check(image.ok&&image.relationships_available&&image.header_size==header&&image.selector_version==0x12345678&&image.selector_oem==0x9a,"Both released V40 headers and current ROM ownership/selector parsed");
  check(image.relationships[0].qualified&&image.relationships[0].kind==1&&image.policies[9].metadata->type==30&&image.policies[9].metadata->unit==1,"BIOS class translation and unit flag only bit0, unknown flags preserved");
  check(report.available&&report.relationships.size()==2&&report.relationships[0].weight_raw==4096&&report.relationships[1].weight_raw==6144,"INFO sparse alternate target indices decoded independently");
  check(report.linked_references.size()==1&&report.linked_references[0].available&&report.linked_references[0].relationship==31&&report.range_references[0].available,"Configured type32 links and type33 ranges decoded");
  const auto source=std::find_if(report.source_references.begin(),report.source_references.end(),[](const auto& r){return r.policy==0;});check(source!=report.source_references.end()&&source->available&&source->enabled==true&&source->relationships==std::vector<uint32_t>{0},"Configured type0 enabled reference range decoded");
  auto plan=plan_power_relationship_weights(c.image,c.identity,layout,c.info,{{0,1.25,4096},{31,0,6144}});check(plan.ok&&plan.patches.size()==2&&plan.selected.size()==2,"Whole selected image and INFO matching produces two typed weight spans");
  check(c.image==original&&c.info==info,"Pure planning never modifies input evidence");
  if(!plan.ok)continue;check(plan.patches[0].offset==image.relationships[0].offset+2&&plan.patches[0].before.size()==2&&plan.patches[0].after.size()==2,"Selected projection is exact little-endian H span");
  std::set<uint32_t> allowed;for(const auto& p:plan.patches){allowed.insert(p.offset);allowed.insert(p.offset+1);}bool preserved=true;for(uint32_t i=0;i<original.size();++i)if(original[i]!=(*plan.expected_image)[i]&&!allowed.count(i))preserved=false;check(preserved,"Only selected weights differ; checksum/object/unknown bytes are not repaired");
  check(verify_power_relationship_weights(plan,c.identity,layout,*plan.expected_info,plan.expected_image.get()).ok,"Exact expected image and full INFO verify");
  check(verify_power_relationship_weights(plan,c.identity,layout,c.info,&c.image,true).ok,"Original full INFO and image verify for explicit removal proof");
  auto changed=*plan.expected_info;changed[0x5000]^=1;check(!verify_power_relationship_weights(plan,c.identity,layout,changed).ok,"Unselected unknown INFO change invalidates activation proof");
  auto changed_image=*plan.expected_image;changed_image[0x700]^=1;check(!verify_power_relationship_weights(plan,c.identity,layout,*plan.expected_info,&changed_image).ok,"Complete image verification includes unselected bytes");
  auto other=c.identity;other.subsystem_id^=1;check(!verify_power_relationship_weights(plan,other,layout,*plan.expected_info).ok,"Changed adapter identity refused");
  auto forged=plan;forged.patches.clear();forged.selected[0].after_raw=0;check(verify_power_relationship_weights(forged,c.identity,layout,*plan.expected_info,plan.expected_image.get()).ok,"Public review projection cannot forge authoritative plan");
 }
 auto c=power_case();auto changed=c.info;changed[0x41d0+31*0x24]=7;auto report=decode_power_relationships(layout,changed);check(report.available&&!report.relationships[1].weight_raw&&report.relationships[1].target_metadata.has_value(),"Unknown relationship kinds retain target inspection but not weights");
 check(plan_power_relationship_weights(c.image,c.identity,layout,changed,{{0,1.5,{}}}).ok,"Unrelated unknown relationship does not remove a qualified selection");
 changed=c.info;dword(changed,0xcc+9*0x104+4,30|(9u<<8)|(8u<<16));report=decode_power_relationships(layout,changed);check(report.available&&!report.relationships[0].target_metadata&&report.relationships[1].target_metadata.has_value(),"Unknown target units independent from unrelated valid targets");
 check(!plan_power_relationship_weights(c.image,c.identity,layout,changed,{{0,1.5,{}}}).ok,"Selected unknown target unit refuses projection");
 changed=c.info;word(changed,0xcc+3*0x104+0x34,0xffff);word(changed,0xcc+3*0x104+0x36,0);report=decode_power_relationships(layout,changed);check(report.range_references[0].available&&report.range_references[0].relationships.empty(),"Reversed type33 range is empty valid, not a sentinel");
 changed=c.info;word(changed,0xcc+0xbc,1);word(changed,0xcc+0xbe,0);report=decode_power_relationships(layout,changed);check(!report.source_references[0].available,"Enabled reversed type0 range remains invalid");
 changed=c.info;changed[0xcc+0xb9]=2;report=decode_power_relationships(layout,changed);check(!report.source_references[0].available&&!report.source_references[0].enabled,"Unknown type0 flag does not invent enable state");
 changed=c.info;changed.resize(20511);check(!decode_power_relationships(layout,changed).available,"Truncated INFO rejected");auto wrong_layout=layout;wrong_layout.policy_stride=0x100;check(!decode_power_relationships(wrong_layout,c.info).available,"Only sourced INFO geometry accepted");
 check(!plan_power_relationship_weights(c.image,c.identity,layout,c.info,{{0,1,{}},{0,2,{}}}).ok,"Duplicate selected relationships rejected before projection");check(!plan_power_relationship_weights(c.image,c.identity,layout,c.info,{{32,1,{}}}).ok,"Out-of-capacity selection rejected");
 check(!plan_power_relationship_weights(c.image,c.identity,layout,c.info,{{0,1,4000}}).ok,"Stale expected raw weight rejected");changed=c.info;dword(changed,0xcc+9*0x104+12,44000);check(!plan_power_relationship_weights(c.image,c.identity,layout,changed,{{0,1,{}}}).ok,"All target limits must match image exactly");
 auto short_image=c.image;short_image.resize(object_vfe+0x38+32*0x66+10);check(!inspect_power_weight_image(short_image).relationships_available,"Truncated relationship extent is not writable");
 for(const auto multiplier:{-1.0,16.0,0.1,std::numeric_limits<double>::infinity(),std::numeric_limits<double>::quiet_NaN()}){bool refused=false;try{power_weight_raw(multiplier);}catch(const std::exception&){refused=true;}check(refused,"Nonfinite/out-of-range/inexact Q4.12 refused");}
 check(power_weight_raw(0)==0&&power_weight_raw(65535.0/4096)==65535&&power_weight_raw(1)==4096,"Q4.12 endpoints and unity exact");
 c.identity.architecture=10;c.identity.device_id=0x1234;c.identity.subsystem_id=0x76543210;c.identity.slot="0000:7b:01.1";for(const auto at:{0x86u,0x1086u,0x2706u,0x3086u})word(c.image,at,0x1234);dword(c.image,0x220c,0x1234);check(plan_power_relationship_weights(c.image,c.identity,layout,c.info,{{0,1,{}}}).ok,"Alternate current architecture/identity not selected by board or driver lists");
}
}
int main(){power_tests();short_v30_tests();legacy_info_tests();rm26_tests();read_only_table_tests();modern_pairing_tests();std::printf("Power relationships: %d checks, %d failures\n",checks,failures);return failures?1:0;}
