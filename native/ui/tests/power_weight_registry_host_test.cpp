// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_weight_registry_host.h"
#include <cstdio>
#include <cstdlib>
using namespace druta::ui;
#define CHECK(x) do{if(!(x)){std::fprintf(stderr,"%d: %s\n",__LINE__,#x);std::abort();}}while(false)
int main(){VoltageScaffoldWindowsTarget t{"0000:09:00.0","PCI\\VEN_10DE&DEV_1234&SUBSYS_12345678\\A","SYSTEM\\CurrentControlSet\\Control\\Class\\{4d36e968-e325-11ce-bfc1-08002be10318}\\0007"};int calls=0;PowerWeightRegistryReader reader=[&](const std::wstring& path){++calls;CHECK(path.find(L"0007")!=std::wstring::npos);return PowerWeightRegistrySnapshot{{L"romOverride00",3,{1,2}},{L"EnableGpuFirmware",4,{0x11}}};};auto got=read_power_weight_registry(t,reader);CHECK(calls==2&&got.size()==2);
 auto bad=t;bad.driver_key="SYSTEM\\Other";bool invalid=false;try{read_power_weight_registry(bad,reader);}catch(const VoltageEquationEvidenceError& e){invalid=e.kind==VoltagePolicyAvailability::invalid;}CHECK(invalid&&calls==2);
 calls=0;bool transient=false;try{read_power_weight_registry(t,[&](const std::wstring&){++calls;return PowerWeightRegistrySnapshot{{L"vbios",3,{static_cast<uint8_t>(calls)}}};});}catch(const VoltageEquationEvidenceError& e){transient=e.kind==VoltagePolicyAvailability::transient;}CHECK(transient&&calls==2);
 invalid=false;try{read_power_weight_registry(t,[](const std::wstring&){return PowerWeightRegistrySnapshot{{L"VBIOS",3,{}},{L"vbios",3,{}}};});}catch(const VoltageEquationEvidenceError& e){invalid=e.kind==VoltagePolicyAvailability::invalid;}CHECK(invalid);std::puts("Power weight registry host tests passed");}
