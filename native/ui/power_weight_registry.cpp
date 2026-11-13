// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_weight_registry.h"
#include <algorithm>
#include <cwctype>
#include <map>
#include <stdexcept>
namespace druta::ui {
namespace {
void require(bool ok,const char* msg){if(!ok)throw std::runtime_error(msg);}
std::wstring fold(const std::wstring& name){std::wstring out;out.reserve(name.size());for(wchar_t c:name)out.push_back((c>=L'A'&&c<=L'Z'?static_cast<wchar_t>(c+(L'a'-L'A')):c));return out;}
using Names=std::map<std::wstring,size_t>;
Names names(const PowerWeightRegistrySnapshot& s){Names result;for(size_t i=0;i<s.size();++i){require(s[i].name.find(L'\0')==std::wstring::npos,"Invalid registry name");require(result.emplace(fold(s[i].name),i).second,"Case-insensitive registry name conflict");}return result;}
int hex(wchar_t c){return c>=L'0'&&c<=L'9'?c-L'0':c>=L'a'&&c<=L'f'?c-L'a'+10:-1;}
uint32_t u32le(const uint8_t* p){return uint32_t(p[0])|(uint32_t(p[1])<<8)|(uint32_t(p[2])<<16)|(uint32_t(p[3])<<24);}
bool equal(const PowerWeightRegistryValue* a,const PowerWeightRegistryValue* b){return (!a&&!b)||(a&&b&&a->type==b->type&&a->bytes==b->bytes);}
}
PowerWeightRegistryProjection project_power_weight_registry(const PowerWeightRegistrySnapshot& snapshot,const std::vector<uint8_t>& image,uint32_t version,uint8_t oem,const std::vector<PowerWeightRegistryPatch>& patches){
 require(version!=0,"Invalid ROM selector");const auto map=names(snapshot);std::map<unsigned,size_t> records;
 for(const auto& [name,index]:map){if(name.size()!=13||name.substr(0,11)!=L"romoverride")continue;const int h=hex(name[11]),l=hex(name[12]);if(h<0||l<0)continue;require(snapshot[index].type==3,"Existing ROM override is not binary");records.emplace(unsigned(h*16+l),index);}
 for(unsigned i=0;i<records.size();++i)require(records.count(i)!=0,"Existing ROM override records have a gap");
 auto effective=image;const auto vb=map.find(L"vbios");if(vb!=map.end()){const auto& value=snapshot[vb->second];require(value.type==3&&value.bytes.size()==image.size(),"Existing VBIOS override malformed");effective=value.bytes;}
 for(const auto& [index,pos]:records){(void)index;const auto& raw=snapshot[pos].bytes;require(raw.size()>=10&&raw.size()<=256&&raw.size()==10u+raw[9],"Existing ROM override record malformed");const uint32_t selected=uint32_t(raw[0])<<24|uint32_t(raw[1])<<16|uint32_t(raw[2])<<8|raw[3];if(selected&&(selected!=version||(raw[4]&&raw[4]!=oem)))continue;const auto at=u32le(raw.data()+5);require(at<=effective.size()&&raw[9]<=effective.size()-at,"Existing ROM override outside image");std::copy(raw.begin()+10,raw.end(),effective.begin()+at);}
 require(effective==image,"Existing ROM overrides differ from current image");PowerWeightRegistryProjection result{snapshot,image};std::vector<bool> touched(image.size());unsigned next=static_cast<unsigned>(records.size());
 for(const auto& patch:patches){const size_t at=patch.offset,n=patch.before.size();require(n&&n==patch.after.size()&&at<=image.size()&&n<=image.size()-at,"Invalid selected patch extent");require(std::equal(patch.before.begin(),patch.before.end(),image.begin()+at),"Selected patch baseline changed");for(size_t i=0;i<n;++i){require(!touched[at+i],"Selected patches overlap");touched[at+i]=true;}std::copy(patch.after.begin(),patch.after.end(),result.expected_image.begin()+at);
  for(size_t start=0;start<n;start+=246){require(next<256,"No free ROM override record slots");const auto count=std::min<size_t>(246,n-start);const auto offset=uint32_t(at+start);std::vector<uint8_t> raw={uint8_t(version>>24),uint8_t(version>>16),uint8_t(version>>8),uint8_t(version),oem,uint8_t(offset),uint8_t(offset>>8),uint8_t(offset>>16),uint8_t(offset>>24),uint8_t(count)};raw.insert(raw.end(),patch.after.begin()+start,patch.after.begin()+start+count);const wchar_t* digits=L"0123456789ABCDEF";std::wstring name=L"romOverride";name+=digits[next>>4];name+=digits[next&15];result.after.push_back({std::move(name),3,std::move(raw)});++next;}}
 require(next>records.size(),"No selected ROM patches");return result;
}
PowerWeightRegistrySnapshot restore_power_weight_registry(const PowerWeightRegistrySnapshot& before,const PowerWeightRegistrySnapshot& after,const PowerWeightRegistrySnapshot& current){
 const auto b=names(before),a=names(after),c=names(current);std::map<std::wstring,bool> keys;for(const auto& x:b)keys[x.first]=true;for(const auto& x:a)keys[x.first]=true;auto result=current;
 for(const auto& [key,unused]:keys){(void)unused;const auto bi=b.find(key),ai=a.find(key),ci=c.find(key);const auto* old=bi==b.end()?nullptr:&before[bi->second];const auto* next=ai==a.end()?nullptr:&after[ai->second];const auto* now=ci==c.end()?nullptr:&current[ci->second];if(equal(old,next))continue;require(equal(now,old)||equal(now,next),"Owned registry value changed externally");auto found=std::find_if(result.begin(),result.end(),[&](const auto& item){return fold(item.name)==key;});if(!old){if(found!=result.end())result.erase(found);}else if(found==result.end())result.push_back(*old);else{found->type=old->type;found->bytes=old->bytes;}}
 return result;
}
}
