// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#define main unused_offsets_fixture_main
#include "voltage_offsets_service_test.cpp"
#undef main
#include "voltage_tables_fake.h"
int main(){
 reset();voltage_tables_test::Fake card;card.id=identity;auto table_api=voltage_tables_test::api();table_api.offsets_api=api();VoltageTablesService service(table_api,card.gpu(),card.id);
 CHECK(service.read(3).requests_complete&&service.state().offsets_recognized&&sets==0&&card.sets==0);
 auto find=[&](VoltageTableFamily family){for(const auto& row:service.state().rows)if(row.key.family==family&&row.key.rail==0)return row;std::abort();};
 auto off=find(VoltageTableFamily::offsets);auto req=find(VoltageTableFamily::requests);CHECK(off.writable&&off.value&&req.writable&&req.value);
 VoltageTableRequest signed_request{off.key,-5000,off.binding,off.value};VoltageTableRequest rail_request{req.key,*req.value+300,req.binding,req.value};
 CHECK(!service.preview({signed_request,rail_request}).ok&&sets==0&&card.sets==0);
 CHECK(service.apply(rail_request).ok&&card.sets==1&&service.state().undo_available);
 CHECK(service.apply(signed_request).ok&&sets==1&&card.sets==1&&value(6)==-5000&&service.state().offset_undo_available&&service.state().undo_available);
 CHECK(service.undo(2).ok&&value(6)==0&&card.sets==1&&service.state().undo_available);
 CHECK(service.undo(1).ok&&card.value(0,req.key.field)==*req.value&&card.sets==2);
 service.read(2);off=find(VoltageTableFamily::offsets);auto doc=capture_voltage_tables_profile(service.state(),{off.key});auto staged=stage_voltage_tables_profile(doc,service.state());CHECK(staged.size()==1&&staged[0].key.family==VoltageTableFamily::offsets);
 signed_request={off.key,-7000,off.binding,off.value};const auto plan=service.preview({signed_request});CHECK(plan.ok);VoltageTablesReceipt receipt;int journals=0;const int before=sets;CHECK(service.apply_profile(plan,[&](const auto& r){receipt=r;++journals;CHECK(sets==before);}).ok&&journals==1&&value(6)==-7000&&card.sets==2);CHECK(service.verify(plan).ok&&service.verify(receipt).ok&&service.restore(receipt).ok&&service.verify(receipt,true).ok&&value(6)==0&&card.sets==2&&locks==0&&card.depth==0);
 std::puts("Voltage table offset bridge passed");return 0;
}
