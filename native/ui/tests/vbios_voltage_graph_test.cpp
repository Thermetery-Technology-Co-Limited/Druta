// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Synthetic bounded fixtures adapted from fa6ee83 tests/test_vbios_fields.py.
#include "vbios_voltage_graph.h"
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <utility>
using namespace druta::ui;
namespace {
int checks=0,failures=0;
void check(bool value,const char* message){++checks;if(!value){++failures;std::printf("FAIL: %s\n",message);}}
void word(std::vector<uint8_t>& b,size_t at,uint16_t value){b.at(at)=uint8_t(value);b.at(at+1)=uint8_t(value>>8);}
void dword(std::vector<uint8_t>& b,size_t at,uint32_t value){for(unsigned i=0;i<4;++i)b.at(at+i)=uint8_t(value>>(8*i));}
void number(std::vector<uint8_t>& b,size_t at,float value){uint32_t raw=0;std::memcpy(&raw,&value,4);dword(b,at,raw);}
constexpr uint32_t rail=0x800,legacy_vfe=0x2000,object_vfe=0x4000,relative_base=14+29*255;
void image(std::vector<uint8_t>& b,uint32_t start,uint32_t size,uint8_t kind,uint16_t device,bool npds=false,uint16_t header=0x80){
 b.at(start)=uint8_t(npds?'V':0x55);b.at(start+1)=uint8_t(npds?'N':0xaa);word(b,start+24,header);const auto at=start+header;const char* magic=npds?"NPDS":"PCIR";std::memcpy(b.data()+at,magic,4);
 word(b,at+4,0x10de);word(b,at+6,device);word(b,at+10,24);word(b,at+16,uint16_t(size/512));b.at(at+20)=kind;b.at(at+21)=128;
}
void checksum(std::vector<uint8_t>& b){b[0x10b]=0;unsigned sum=0;for(size_t i=0x100;i<0x10c;++i)sum+=b[i];b[0x10b]=uint8_t(0u-sum);}
std::vector<uint8_t> fixture(bool object=false,uint16_t device=0x1234){
 std::vector<uint8_t> b(object?0x8000:0x6000);const auto vfe=object?object_vfe:legacy_vfe;
 image(b,0,object?0x1000:0x6000,0,device);
 if(object){image(b,0x1000,0x1000,3,device);image(b,0x2000,0x1000,0xe0,device,false,0x700);image(b,0x3000,0x9000,0xe0,device,true);
  const uint32_t directory=0x2200,count=3,base=directory+16+60*count;dword(b,directory,3);dword(b,directory+4,16);dword(b,directory+8,count);dword(b,directory+12,device);
  const uint32_t ids[]={0xfb,0xbe,0x14},offsets[]={base,0x2700,vfe},sizes[]={8,24,relative_base+25*6+5*16};
  for(uint32_t i=0;i<count;++i){const auto at=directory+16+60*i;dword(b,at,ids[i]);dword(b,at+4,offsets[i]-base);dword(b,at+8,sizes[i]);}
 }
 const uint8_t signature[]={0xff,0xb8,'B','I','T',0};std::copy_n(signature,6,b.begin()+0x100);b[0x108]=12;b[0x109]=6;b[0x10a]=3;
 const auto token=[&](uint32_t at,uint8_t id,uint16_t size,uint16_t ptr){b[at]=id;b[at+1]=2;word(b,at+2,size);word(b,at+4,ptr);};
 token(0x10c,'P',0x130,0x200);token(0x112,'i',5,0x360);token(0x118,'C',0x10,0x380);checksum(b);dword(b,0x360,0x12345678);b[0x364]=0x9a;
 dword(b,0x220,object?vfe-0x1000:vfe);dword(b,0x268,rail);
 std::fill_n(b.begin()+rail,70,uint8_t{255});const uint8_t rail_header[]={0x30,5,65,1,0};std::copy_n(rail_header,5,b.begin()+rail);b[rail+5]=b[rail+6]=1;
 for(const auto pair:{std::pair<uint32_t,uint16_t>{13,0},{15,1},{17,2},{33,3},{19,4}})word(b,rail+5+pair.first,pair.second);
 const uint8_t header[]={0x30,14,29,255,25,6};std::copy_n(header,6,b.begin()+vfe);b[vfe+11]=5;word(b,vfe+12,16);
 const float values[]={1000000,20000,1200000,900000,750000,-100000};const auto base=vfe+relative_base;
 for(uint32_t i=0;i<6;++i){const auto row=base+25*i;b[row]=1;b[row+2]=b[row+24]=255;b[row+23]=2;number(b,row+3,values[i]);number(b,row+7,values[i]);number(b,row+11,values[i]);}
 b[base]=7;b[base+2]=5;b[base+24]=0;number(b,base+3,900000);number(b,base+7,1120000);dword(b,base+11,2u<<16);
 const auto terms=base+25*6;for(uint32_t i=0;i<8;++i){b[terms+5*i]=i<2?2:1;if(i<2)dword(b,terms+5*i+1,i);else number(b,terms+5*i+1,i==2?1000000.0f:0.0f);}
 b[base+25+2]=b[base+25+24]=0;return b;
}
const VbiosVoltageRoleBinding& role(const VbiosVoltageGraph& graph,VbiosVoltageRole wanted,uint32_t index=0){for(const auto& value:graph.roles)if(value.role==wanted&&value.rail==index)return value;throw std::runtime_error("Missing test role");}
void clock(std::vector<uint8_t>& b,uint32_t equation,uint8_t kind=1,uint8_t version=0x40){dword(b,0x38c,0x900);b[0x900]=version;b[0x901]=8;b[0x902]=version==0x40?15:23;b[0x903]=1;b[0x908]=kind;b[0x90b]=uint8_t(equation);b[0x915]=uint8_t(equation>>8);if(version==0x50)word(b,0x90b,uint16_t(equation));}
void run(){
 for(const bool object:{false,true}){const auto bytes=fixture(object,object?0x99aa:0x2345);const auto original=bytes;const auto graph=parse_vbios_voltage_graph(bytes);
  check(graph.ok&&graph.graph_complete&&graph.clock_roots_complete,"Alternate identity/legacy and EFI object layout parsed");check(bytes==original,"Pure inspection leaves complete input unchanged");
  check(graph.selector_version==0x12345678&&graph.selector_oem==0x9a&&graph.equations.size()==6&&graph.variable_rows.size()==255,"Selector and complete typed equation/variable inventory preserved");
  check(graph.vfe_offset==(object?object_vfe:legacy_vfe)&&graph.objects.size()==(object?3u:0u),"EFI relocation and object provenance retained");
  const auto& rel=role(graph,VbiosVoltageRole::rel);check(rel.encoding_qualified&&rel.shared&&rel.stored_lower==900000&&rel.stored_value==1120000,"Shared REL type7 bounds remain an understood encoding");
  check(rel.dependencies==std::vector<uint32_t>({0,5})&&rel.affected==std::vector<uint32_t>({0,1})&&rel.affected_rail_roots.size()==2,"Complete forward/reverse REL and ALT dependencies retained");
  check(graph.equations[0].variables.size()==2&&graph.equations[0].terms.size()==6&&graph.equations[0].next==5&&graph.equations[0].output==2,"Typed polynomial variables/coefficient terms and immutable selectors exposed");
  check(rel.rel_range&&(*rel.rel_range)[0]==800000&&(*rel.rel_range)[1]==1020000,"REL conservative range includes chained signed tail");
  check(role(graph,VbiosVoltageRole::alt).encoding_qualified&&role(graph,VbiosVoltageRole::ov).encoding_qualified&&role(graph,VbiosVoltageRole::vmin).encoding_qualified&&!role(graph,VbiosVoltageRole::info_vmin).encoding_qualified,"Independent constant/addend/VMIN encoding and informational selector distinction");
 }
 auto b=fixture();b[0x10b]^=1;check(!parse_vbios_voltage_graph(b).ok,"BIT checksum failure rejected");
 b=fixture();b[0x118]='P';check(!parse_vbios_voltage_graph(b).ok,"Duplicate BIT token rejected");
 b=fixture();dword(b,0x220,0xffffff00);check(!parse_vbios_voltage_graph(b).ok,"Out-of-capture pointer rejected without overflow");
 b=fixture(true);dword(b,0x2210+120,0x15);check(!parse_vbios_voltage_graph(b).ok,"VFE expected object owner required");
 b=fixture(true);dword(b,0x2214,object_vfe-(0x2210+3*60));dword(b,0x2218,relative_base+25*6+5*16);check(!parse_vbios_voltage_graph(b).ok,"Overlapping metadata owners cannot qualify a selected table");
 b=fixture(true);dword(b,0x220,0x1000);check(!parse_vbios_voltage_graph(b).ok,"Relocated pointer into non-VFE metadata is refused");
 b=fixture(true);b.resize(object_vfe+relative_base+25*6-1);check(!parse_vbios_voltage_graph(b).ok,"Truncated selected equations rejected even if archival tail may be absent");
 b=fixture(true);word(b,0x1084,0x4444);check(!parse_vbios_voltage_graph(b).ok,"EFI device pairing mismatch rejected");
 b=fixture();const auto base=legacy_vfe+relative_base;word(b,rail+5+35,2);auto graph=parse_vbios_voltage_graph(b);
 check(graph.ok&&role(graph,VbiosVoltageRole::ov).shared&&!role(graph,VbiosVoltageRole::ov).encoding_qualified,"Unnamed word selector alias disqualifies shared constant");
 b=fixture();b[rail+5+21]=2;graph=parse_vbios_voltage_graph(b);check(graph.ok&&role(graph,VbiosVoltageRole::ov).shared&&!role(graph,VbiosVoltageRole::ov).encoding_qualified,"Possible byte alias is a conservative dependency");
 b=fixture();clock(b,0);graph=parse_vbios_voltage_graph(b);check(graph.ok&&graph.clock_roots_complete&&role(graph,VbiosVoltageRole::rel).affected_clock_roots.size()==1&&role(graph,VbiosVoltageRole::rel).encoding_qualified,"Known clock dependency retained without pretending REL is unshared");
 b=fixture();clock(b,2,3,0x50);graph=parse_vbios_voltage_graph(b);check(graph.ok&&graph.clock_roots_complete&&!role(graph,VbiosVoltageRole::ov).encoding_qualified,"v50 DRAM clock root conservatively shares selected constant");
 b=fixture();clock(b,0,2);graph=parse_vbios_voltage_graph(b);check(graph.ok&&!graph.clock_roots_complete&&!graph.clock_error.empty()&&!role(graph,VbiosVoltageRole::rel).encoding_qualified,"Unknown active clock consumer retains inspection but forbids qualification");
 b=fixture();clock(b,6);graph=parse_vbios_voltage_graph(b);check(graph.ok&&!graph.clock_roots_complete,"Dangling clock root is explicit incomplete external sharing");
 b=fixture();b[base+25*5+2]=0;b[base+25*5+24]=0;graph=parse_vbios_voltage_graph(b);check(graph.ok&&!graph.graph_complete&&!graph.graph_error.empty()&&!role(graph,VbiosVoltageRole::rel).encoding_qualified,"Cycle retained diagnostically with no qualified role");
 b=fixture();b[base+2]=6;b[base+24]=0;graph=parse_vbios_voltage_graph(b);check(graph.ok&&!graph.graph_complete,"Dangling next edge disqualifies graph");
 b=fixture();b[base+25*5]=6;graph=parse_vbios_voltage_graph(b);check(graph.ok&&!graph.graph_complete,"Unknown active equation kind disqualifies whole sharing graph");
 b=fixture();const auto term=base+25*6;b[term+5*2]=3;word(b,term+5*2+1,2);graph=parse_vbios_voltage_graph(b);check(graph.ok&&graph.graph_complete&&!role(graph,VbiosVoltageRole::ov).encoding_qualified&&graph.equations[2].referenced_by==std::vector<uint32_t>{0},"Polynomial coefficient equation references participate in reverse sharing");
 b=fixture();b[term+5*2]=1;dword(b,term+5*2+1,0x7f800000);graph=parse_vbios_voltage_graph(b);check(graph.ok&&!graph.graph_complete,"Nonfinite polynomial literal invalidates graph");
 b=fixture();b[legacy_vfe+11]=4;graph=parse_vbios_voltage_graph(b);check(graph.ok&&!graph.graph_complete,"Unknown term stride remains unqualified");
 b=fixture();const std::vector<uint8_t> saved_terms(b.begin()+term,b.begin()+term+5*16);
 std::fill(b.begin()+base+25*6,b.begin()+base+25*300,uint8_t{0});std::copy(saved_terms.begin(),saved_terms.end(),b.begin()+base+25*300);
 b[legacy_vfe+5]=44;b[legacy_vfe+10]=1;const auto high=base+25*299;
 b[high]=1;b[high+2]=b[high+24]=255;b[high+23]=2;number(b,high+3,10000);number(b,high+7,10000);number(b,high+11,10000);
 const auto high_terms=base+25*300;b[high_terms+5*2]=3;word(b,high_terms+5*2+1,299);b[high_terms+5*2+4]=0xa5;
 b[high_terms+3]=0xf0;graph=parse_vbios_voltage_graph(b);
 check(graph.ok&&graph.graph_complete&&graph.equations.size()==300&&graph.equations[299].referenced_by==std::vector<uint32_t>{0},"High-index equation counts and polynomial references use full little-endian selectors");
 check(graph.equations[0].terms[0].value==299&&graph.equations[0].terms[0].raw[4]==0xa5&&graph.equations[0].variables[0].raw[3]==0xf0,"Opaque polynomial descriptor bytes preserved, not interpreted as extra selector bits");
 b=fixture();b[rail+3]=2;std::copy_n(b.begin()+rail+5,65,b.begin()+rail+70);b[rail+71]=2;graph=parse_vbios_voltage_graph(b);
 check(graph.ok&&role(graph,VbiosVoltageRole::rel,1).encoding_qualified&&role(graph,VbiosVoltageRole::rel).affected_rail_roots.size()==4&&!role(graph,VbiosVoltageRole::ov,1).encoding_qualified,"Shared REL across two current rails supported as encoding; aliased constants not qualified");
 check(!parse_vbios_voltage_graph({}).ok,"Empty ROM refused");b.resize(vbios_voltage_max_image_bytes+1);check(!parse_vbios_voltage_graph(b).ok,"16MiB parser bound independent of capture getter limit");
}
}
int main(){run();std::printf("VBIOS voltage graph: %d checks, %d failures\n",checks,failures);return failures?1:0;}
