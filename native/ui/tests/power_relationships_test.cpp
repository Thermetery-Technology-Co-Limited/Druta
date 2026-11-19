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
int main(){power_tests();std::printf("Power relationships: %d checks, %d failures\n",checks,failures);return failures?1:0;}
