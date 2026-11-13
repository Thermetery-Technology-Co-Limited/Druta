// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_weight_registry.h"
#include <cstdio>
#include <cstdlib>
using namespace druta::ui;
#define CHECK(x) do{if(!(x)){std::fprintf(stderr,"%d: %s\n",__LINE__,#x);std::abort();}}while(false)
template<class F>void rejects(F f){bool bad=false;try{f();}catch(const std::exception&){bad=true;}CHECK(bad);}
int main(){std::vector<uint8_t> image(600);for(size_t i=0;i<image.size();++i)image[i]=uint8_t(i);PowerWeightRegistrySnapshot before={{L"EnableGpuFirmware",4,{17,0,0,0}},{L"Unrelated",7,{9,8}}};
 auto plan=project_power_weight_registry(before,image,0x12345678,0xab,{{258,{2,3},{9,10}}});CHECK(plan.after.size()==3&&plan.after[0].bytes==before[0].bytes&&plan.after[2].name==L"romOverride00"&&plan.after[2].bytes==std::vector<uint8_t>({0x12,0x34,0x56,0x78,0xab,2,1,0,0,2,9,10})&&plan.expected_image[258]==9);
 auto current=plan.after;current[1].bytes={4};current.push_back({L"Other",1,{3}});auto restored=restore_power_weight_registry(before,plan.after,current);CHECK(restored.size()==3&&restored[1].bytes==std::vector<uint8_t>({4})&&restored[2].name==L"Other");
 current=plan.after;current[2].bytes[10]^=1;rejects([&]{restore_power_weight_registry(before,plan.after,current);});
 auto aliases=before;aliases.push_back({L"UNRELATED",1,{}});rejects([&]{restore_power_weight_registry(before,plan.after,aliases);});
 auto gap=before;gap.push_back({L"romOverride01",3,{}});rejects([&]{project_power_weight_registry(gap,image,1,0,{{0,{0},{1}}});});
 rejects([&]{project_power_weight_registry(before,image,1,0,{{0,{0,1},{2,3}},{1,{1},{4}}});});
 std::vector<uint8_t> large(500,1),change(500,2);auto chunked=project_power_weight_registry({},large,1,0,{{0,large,change}});CHECK(chunked.after.size()==3&&chunked.after[0].bytes.size()==256&&chunked.after[1].bytes.size()==256&&chunked.after[2].bytes.size()==18);
 PowerWeightRegistrySnapshot old={{L"Existing",4,{1,2}}},applied={{L"EXISTING",3,{3}}},now={{L"existing",3,{3}}};auto undo=restore_power_weight_registry(old,applied,now);CHECK(undo[0].name==L"existing"&&undo[0].type==4&&undo[0].bytes==std::vector<uint8_t>({1,2}));
 std::puts("Power weight registry projection tests passed");}
