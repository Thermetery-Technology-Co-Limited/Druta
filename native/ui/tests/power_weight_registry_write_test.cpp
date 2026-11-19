// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_weight_registry_write.h"
#include <algorithm>
#include <cstdio>
#include <cstdlib>
using namespace druta::ui;
#define CHECK(x) do{if(!(x)){std::fprintf(stderr,"%d: %s\n",__LINE__,#x);std::abort();}}while(false)
int main(){VoltageScaffoldWindowsTarget target{"0000:09:00.0","PCI\\VEN_10DE&DEV_1234&SUBSYS_12345678\\A","SYSTEM\\CurrentControlSet\\Control\\Class\\{4d36e968-e325-11ce-bfc1-08002be10318}\\0007"};PowerWeightRegistrySnapshot before={{L"EnableGpuFirmware",4,{1}}},after=before;after.push_back({L"romOverride00",3,{0,0,0,1,0,0,0,0,0,1,7}});auto current=before;int writes=0,journals=0;bool reject=false,fail_write=false,lie=false;PowerWeightRegistryWriteApi api;
 api.read=[&](const std::wstring&){return current;};api.write=[&](const std::wstring&,const std::wstring& name,const std::optional<PowerWeightRegistryValue>& v,std::string&){++writes;CHECK(journals==writes);if(v){if(!lie)current.push_back(*v);}else{current.erase(std::remove_if(current.begin(),current.end(),[&](const auto& x){return x.name==name;}),current.end());}return !fail_write;};
 const auto journal=[&](const std::wstring&,std::string&){++journals;return !reject;};reject=true;auto result=write_power_weight_registry(target,before,after,journal,api);CHECK(!result.ok&&!result.may_have_changed&&writes==0);reject=false;journals=0;result=write_power_weight_registry(target,before,after,journal,api);CHECK(result.ok&&result.may_have_changed&&writes==1&&current.size()==2);
 journals=writes=0;result=write_power_weight_registry(target,after,before,journal,api);CHECK(result.ok&&result.may_have_changed&&writes==1&&current.size()==1);
 journals=writes=0;fail_write=true;result=write_power_weight_registry(target,before,after,journal,api);CHECK(!result.ok&&result.may_have_changed&&writes==1&&current.size()==2);fail_write=false;current=before;journals=writes=0;lie=true;result=write_power_weight_registry(target,before,after,journal,api);CHECK(!result.ok&&result.may_have_changed&&writes==1);lie=false;
 current=before;auto invalid=after;invalid[0].bytes={2};journals=writes=0;result=write_power_weight_registry(target,before,invalid,journal,api);CHECK(!result.ok&&!result.may_have_changed&&writes==0&&journals==0);
 current=before;current[0].bytes={3};result=write_power_weight_registry(target,before,after,journal,api);CHECK(!result.ok&&!result.may_have_changed&&writes==0);
 std::puts("Power weight registry writes passed");}
