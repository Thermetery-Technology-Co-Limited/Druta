// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#define main established_full_fields_fixture_entry
#include "core_client_test.cpp"
#undef main
#include "python_full_profile.h"
namespace {
Command full_command(const ProfileJson& source,const std::vector<std::string>& selection,bool preview=false){Command c;c.operation=preview?Command::Operation::preflight_catalog_profile:Command::Operation::apply_catalog_profile;c.catalog_profile=std::make_shared<const ProfileJson>(select_python_full_profile(source,selection));return c;}
#if defined(_MSC_VER)
__declspec(noinline)
#endif
void full_fields(){
 auto a=api();a.lock=lock_gpu;a.unlock=unlock_gpu;a.telemetry=memory_telemetry;a.clkdom_ok=dom_ok;a.clkdom_layout=dom_layout;a.clkdom_domains=dom_list;a.clkdom_controls=dom_controls;a.clkdom_offered=dom_offered;a.clkdom_requests=dom_read;a.clkdom_set=dom_set;a.rail_cap=rail_cap;a.rail_read=rail_read;a.rail_set=rail_set;a.msvdd_reset=rail_reset;a.clkdom_flags=flags_get;a.clkdom_flags_set=flags_set;
 a.rails.lock=lock_gpu;a.rails.unlock=unlock_gpu;a.rails.identity=identity;a.rails.limits=rail_limits;a.rails.fields=rail_fields;a.rails.initial=rail_initial;a.rails.hold=rail_hold;a.rails.set_raw=rail_raw_set;a.rails.reset=rail_initial_set;
 std::string error;auto client=create_core_backend_for_test(a);has_msvdd=true;xoc_flag=0;CHECK(client->open("0000:02:00.0",error));client->read(true);
 ProfileJson source={{"schema",2},{"device",{{"uuid","GPU-A"},{"slot","0000:02:00.0"}}},{"incomplete",ProfileJson::array()},{"xoc",true},{"rail_limits_uv",{{"1",{{"overvoltage",1234}}}}},{"rail_limits_mv",{{"1",{{"overvoltage",999.0}}}}},{"nvvdd_offset_mv",12.5},{"msvdd_offsets_mv",{{"5",7.25}}},{"clock_domain_offsets_mhz",{{"5",10.25}}}};
 auto writes_before=rail_set_calls,advanced_before=advanced_writes;CHECK(!client->execute(full_command(source,{"rail:1:2"})).ok&&rail_set_calls==writes_before&&xoc_flag==0);
 Command gate;gate.operation=Command::Operation::set_rail_mode;gate.enabled=true;CHECK(client->execute(gate).ok);CHECK(client->execute(full_command(source,{"rail:1:2"},true)).ok&&rail_set_calls==writes_before);
 CHECK(client->execute(full_command(source,{"rail:1:2"})).ok&&raw_limits[1][2]==1234);Command undo;undo.operation=Command::Operation::undo;CHECK(client->execute(undo).ok&&raw_limits[1][2]==700);
 auto absolute=source;absolute.erase("rail_limits_uv");absolute["rail_limits_mv"]["1"]["overvoltage"]=901.234;CHECK(client->execute(full_command(absolute,{"rail:1:2"})).ok&&raw_limits[1][2]==1234);CHECK(client->execute(undo).ok&&raw_limits[1][2]==700);
 CHECK(client->execute(full_command(source,{"voltage:nvvdd-offset"})).ok&&rail_raw[0]==12500);CHECK(client->execute(undo).ok&&rail_raw[0]==0);
 CHECK(!client->execute(full_command(source,{"voltage:msvdd-offset:5"})).ok&&xoc_flag==0);gate.operation=Command::Operation::set_xoc;CHECK(client->execute(gate).ok);
 CHECK(client->execute(full_command(source,{"voltage:msvdd-offset:5"})).ok&&rail_raw[1]==7250);CHECK(client->execute(undo).ok&&rail_raw[1]==0);
 const auto before_fractional=advanced_writes;CHECK(!client->execute(full_command(source,{"clock:domain:5"})).ok&&advanced_writes==before_fractional);source["clock_domain_offsets_mhz"]["5"]=10;CHECK(client->execute(full_command(source,{"clock:domain:5"})).ok&&domain_raw[5]==10000);CHECK(client->execute(undo).ok&&domain_raw[5]==0);
 auto malformed=source;malformed["msvdd_offsets_mv"]["5"]=0.0001;Command bad;bad.operation=Command::Operation::apply_catalog_profile;bad.catalog_profile=std::make_shared<const ProfileJson>(malformed);const auto before_bad=rail_set_calls,before_bad_advanced=advanced_writes;CHECK(!client->execute(bad).ok&&rail_set_calls==before_bad&&advanced_writes==before_bad_advanced&&advanced_writes>advanced_before);
 client->close();has_msvdd=false;
}
}
int main(){full_fields();std::printf("Full tune selected fields: %d checks, %d failures\n",checks,failures);return failures?1:0;}
