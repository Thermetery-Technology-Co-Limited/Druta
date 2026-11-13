// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_tables_fake.h"
#include <cstdlib>
using namespace voltage_tables_test;
#define CHECK(x) do {if(!(x)){std::fprintf(stderr,"%d: %s\n",__LINE__,#x);std::abort();}} while(false)
int main(){
    Fake device;auto adapter=api();int calls=0,mode=0;
    adapter.scaffold_read=[&](dc_gpu* gpu){
        ++calls;CHECK(device.depth>0);
        if(mode==1)throw VoltageEquationEvidenceError("Temporary read failure",VoltagePolicyAvailability::transient);
        if(mode==2)throw VoltageEquationEvidenceError("Receipt changed",VoltagePolicyAvailability::invalid);
        VoltageScaffoldRead read;dc_error error{};bind(gpu,1,&read.target,&error);
        read.receipt_binding=std::string(64,'a');read.image_sha256=std::string(64,'b');read.message="Installed heads recognized (read-only)";
        InstalledVoltageScaffoldHead head;head.rail=0;head.index=42;head.next=7;head.variable=2;head.value_uv=-25000;
        head.role=VbiosVoltageRole::ov;read.heads.push_back(head);
        if(mode==3)++read.target.handle;
        if(mode==4)read.heads.push_back(head);
        return read;
    };
    VoltageTablesService service(adapter,device.gpu(),device.id);CHECK(calls==0);
    CHECK(service.read(1).requests_complete&&calls==0);
    const auto before_reads=device.reads;const auto before_sets=device.sets;
    CHECK(service.read(2).offsets_recognized&&calls==1&&device.reads==before_reads&&device.sets==before_sets);
    const auto find=[&]()->const VoltageTableRow&{for(const auto& row:service.state().rows)if(row.key.family==VoltageTableFamily::offsets)return row;std::abort();};
    CHECK(find().value==-25000&&!find().writable&&!find().stale);
    CHECK(service.state().requests_complete);
    const auto row=find();const VoltageTableRequest request{row.key,0,row.binding,row.value};
    CHECK(!service.apply(request).ok&&device.sets==before_sets);
    const auto saved=capture_voltage_tables_profile(service.state(),{row.key});
    CHECK(saved.at("format")=="druta-native-voltage-offsets"&&stage_voltage_tables_profile(saved,service.state()).front().value==-25000);
    mode=1;CHECK(!service.read(2).offsets_recognized&&find().stale&&find().value==-25000&&find().availability==VoltagePolicyAvailability::transient);
    CHECK(service.state().requests_complete&&calls==2);
    mode=0;CHECK(service.read(2).offsets_recognized&&!find().stale);
    mode=2;CHECK(!service.read(2).offsets_recognized&&find().availability==VoltagePolicyAvailability::invalid&&calls==4);
    mode=3;CHECK(!service.read(2).offsets_recognized&&find().availability==VoltagePolicyAvailability::invalid);
    mode=4;CHECK(!service.read(2).offsets_recognized&&find().availability==VoltagePolicyAvailability::invalid);
    CHECK(device.sets==before_sets&&device.depth==0);
    const auto previous=calls;service.read(0);service.read(8);CHECK(calls==previous);
    std::puts("Installed offset inspection stays independent and read-only");
}
