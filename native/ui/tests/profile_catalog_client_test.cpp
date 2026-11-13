// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Reuse the established injected CoreApi fixtures, without running their
// separate test entry point. No DLL, device, files or helper are opened here.
#define main established_core_fixture_entry
#include "core_client_test.cpp"
#undef main
#include "profile_catalog.h"
#include "curve_profile.h"
static bool catalog_interference=false;
static bool catalog_refresh_throw=false;
static bool catalog_boost_refuse=false;
static dc_status DC_CALL interfering_setter(dc_gpu* gpu,uint32_t type,double value,int32_t preserve,dc_outcome* out){const auto status=setter(gpu,type,value,preserve,out);if(catalog_interference&&type==2)boost_value=99;return status;}
static dc_status DC_CALL throwing_telemetry(dc_gpu* gpu,dc_telemetry* out){if(catalog_refresh_throw){catalog_refresh_throw=false;throw std::runtime_error("one post-restoration telemetry refresh failed");}return memory_telemetry(gpu,out);}
static dc_status DC_CALL refusing_boost(dc_gpu* gpu,int64_t value,dc_outcome* out){if(catalog_boost_refuse){++boost_writes;out->ok=0;return DC_OK;}return boost_set(gpu,value,out);}
static ProfileJson catalog_daily(const char* key,int value){ProfileJson c={{"value",value},{"unit",std::string(key)=="memory"?"MHz eff":std::string(key)=="core"?"MHz":"%"}};if(std::string(key)=="memory")c["scale"]=2;return {{"format","druta-native-daily"},{"version",1},{"target",{{"uuid","GPU-A"},{"slot","0000:02:00.0"}}},{"controls",{{key,c}}}};}
static Command catalog_command(const ProfileJson& document,bool preflight=false){Command c;c.operation=preflight?Command::Operation::preflight_catalog_profile:Command::Operation::apply_catalog_profile;c.catalog_profile=std::make_shared<const ProfileJson>(document);return c;}
static CoreApi catalog_api(){auto a=api();a.lock=lock_gpu;a.unlock=unlock_gpu;a.capabilities=capabilities;a.power=power_read;a.power_bounds=bounds;a.power_set=power_set;a.power_message=message;a.policy_read=policies;a.policy_set=policy_set;a.policy_message=message;a.pins_get=pins_get;a.pins_set=pins_set;return a;}
static ProfileJson catalog_power(bool pinned){
 auto row=[](uint32_t id,bool board_row,const char* unit,uint32_t minimum,uint32_t maximum,uint32_t value){return ProfileJson{{"policy",id},{"type",0},{"channel",0},{"unit",unit},{"minimum",minimum},{"maximum",maximum},{"requested",value},{"board",board_row},{"named_current",false},{"writable",!board_row},{"consistent",!board_row}};};
 return {{"schema",2},{"scope","power_action"},{"saved_at","2026-11-13"},{"saved_ts",1},{"device",{{"name","alternate"},{"vbios",nullptr},{"driver","unlisted"},{"uuid","GPU-A"},{"slot","0000:02:00.0"}}},{"incomplete",ProfileJson::array()},
 {"power_action",{{"board_mw",250000},{"rows",ProfileJson::array({row(0,true,"mW",100000,350000,250000),row(1,false,"mA",0,500,150)})},{"pins",pinned?ProfileJson{{"1",150}}:ProfileJson::object()}}}};
}
int main(){std::string error;Command undo;undo.operation=Command::Operation::undo;
 current=40;boost_value=10;writes=boost_writes=0;mode=0;auto a=catalog_api();
 auto backend=create_core_backend_for_test(a);CHECK(backend->open("0000:02:00.0",error));
 auto memory=catalog_daily("memory",80),boost=catalog_daily("boost",30);auto composed=compose_profiles({{"Memory",memory},{"Boost",boost}});
 CHECK(backend->execute(catalog_command(composed,true)).ok&&writes==0&&boost_writes==0&&!backend->read(false).undo_available);
 CHECK(backend->execute(catalog_command(composed)).ok&&current==80&&boost_value==30);CHECK(backend->read(false).undo_available);
 CHECK(backend->execute(undo).ok&&current==40&&boost_value==10&&!backend->read(false).undo_available);
 // A later capability failure rejects the entire plan before the first write.
 a.boost_set=nullptr;auto missing=create_core_backend_for_test(a);CHECK(missing->open("0000:02:00.0",error));const auto before=writes;
 CHECK(!missing->execute(catalog_command(composed)).ok&&writes==before&&current==40);
 // Conflict checking is independent of setters and catches equal duplicates.
 auto conflict=composed;conflict["components"].push_back(conflict["components"][0]);CHECK(!backend->execute(catalog_command(conflict)).ok&&writes==before);
 auto foreign=boost;foreign["target"]["uuid"]="GPU-other";auto cross=composed;cross["components"][1]["state"]=foreign;CHECK(!backend->execute(catalog_command(cross)).ok&&writes==before);
 // Earlier success plus a later possibly issued failure remains one Undo.
 composed=compose_profiles({{"Boost",boost},{"Memory",memory}});mode=2;auto partial=backend->execute(catalog_command(composed));CHECK(!partial.ok&&partial.may_have_changed&&boost_value==30&&backend->read(false).undo_available);
 mode=0;CHECK(backend->execute(undo).ok&&current==40&&boost_value==10&&!backend->read(false).undo_available);
 // Full Python power state distinguishes unpinned requests from explicit pins.
 board=200000;peer=100;pinned_value=100;pin_present=true;power_writes=policy_writes=pin_writes=0;table_fail=peer_fail=geometry_change=false;
 auto power=catalog_power(false);CHECK(backend->execute(catalog_command(power,true)).ok&&power_writes==0&&pin_writes==0);
 auto applied=backend->execute(catalog_command(power));CHECK(applied.ok&&board==250000&&peer==150&&!pin_present);
 CHECK(backend->execute(undo).ok&&board==200000&&peer==100&&pin_present&&pinned_value==100);
 power=catalog_power(true);CHECK(backend->execute(catalog_command(power)).ok&&peer==150&&pin_present&&pinned_value==150);CHECK(backend->execute(undo).ok&&pinned_value==100);
 auto power_bad=power;power_bad["power_action"]["rows"][1]["channel"]=1;const auto old_power=power_writes;CHECK(!backend->execute(catalog_command(power_bad)).ok&&power_writes==old_power);
 // A saved whole-power state conflicts with a separately selected board row.
 auto native_power=catalog_daily("boost",20);native_power["controls"]={{"power",{{"value",220000},{"unit","mW"}}}};
 ProfileJson conflict_power={{"format","druta-native-combined"},{"version",1},{"components",ProfileJson::array({{{"name","Power"},{"state",power}},{{"name","Board"},{"state",native_power}}})}};
 CHECK(!backend->execute(catalog_command(conflict_power)).ok&&power_writes==old_power);
 // A later curve must qualify the full live curve writer before any memory write.
 auto curve_doc=capture_curve_profile(backend->read(true));curve_doc["points"][0]["delta_khz"]=1000001;
 auto bad_curve=compose_profiles({{"Memory",memory},{"Invalid curve",curve_doc}});const auto before_curve=writes;
 CHECK(!backend->execute(catalog_command(bad_curve)).ok&&writes==before_curve);
 auto no_curve_api=catalog_api();no_curve_api.apply_deltas=nullptr;auto no_curve=create_core_backend_for_test(no_curve_api);CHECK(no_curve->open("0000:02:00.0",error));curve_doc=capture_curve_profile(backend->read(true));
 CHECK(!no_curve->execute(catalog_command(compose_profiles({{"Memory",memory},{"Unavailable curve writer",curve_doc}}))).ok&&writes==before_curve);
 // Final whole-composition verification notices a later writer changing an earlier selected request.
 auto interfering_api=catalog_api();interfering_api.set_offset=interfering_setter;auto interfering=create_core_backend_for_test(interfering_api);CHECK(interfering->open("0000:02:00.0",error));current=40;boost_value=10;catalog_interference=true;
 auto interfered=interfering->execute(catalog_command(compose_profiles({{"Boost",boost},{"Memory",memory}})));CHECK(!interfered.ok&&interfered.may_have_changed&&interfered.message.find("final composition")!=std::string::npos&&boost_value==99);
 catalog_interference=false;CHECK(interfering->execute(undo).ok&&current==40&&boost_value==10);
 CHECK(interfering->execute(catalog_command(compose_profiles({{"Boost",boost},{"Memory",memory}}))).ok);catalog_interference=true;
 auto undo_interfered=interfering->execute(undo);CHECK(!undo_interfered.ok&&boost_value==99&&current==40&&interfering->read(false).undo_available);
 catalog_interference=false;CHECK(interfering->execute(undo).ok&&boost_value==10&&current==40&&!interfering->read(false).undo_available);
 // File order cannot turn grouped power Undo into restoration of intermediate requests.
 policy_external_test=true; // this fixture mode permits ordinary explicit policy writes with an existing selected pin
 for(bool policy_first:{false,true}){board=200000;peer=100;pin_present=true;pinned_value=100;auto pbackend=create_core_backend_for_test(catalog_api());CHECK(pbackend->open("0000:02:00.0",error));Command prepare;prepare.operation=Command::Operation::prepare_scoped_profile;CHECK(pbackend->execute(prepare).ok);auto snapshot=pbackend->read(false).scoped_profiles;CHECK(snapshot!=nullptr);
  auto board_doc=capture_scoped_profile(*snapshot,{ScopedKey::board_power()});board_doc["entries"][0]["value"]=250000;
  auto policy_doc=capture_scoped_profile(*snapshot,{ScopedKey::policy_request(1)});policy_doc["entries"][0]["value"]=180;
  auto collection=policy_first?compose_profiles({{"Policy",policy_doc},{"Board",board_doc}}):compose_profiles({{"Board",board_doc},{"Policy",policy_doc}});
  CHECK(pbackend->execute(catalog_command(collection)).ok&&board==250000&&peer==180&&pin_present&&pinned_value==180);
  CHECK(pbackend->execute(undo).ok&&board==200000&&peer==100&&pin_present&&pinned_value==100&&!pbackend->read(false).undo_available);
 }
 policy_external_test=false;
 // A verified child may consume its temporary stack entry before a refresh
 // throws. Preserve the parent siblings and the older, unrelated Undo below it.
 board=200000;peer=100;pin_present=true;pinned_value=100;boost_value=10;auto refresh_api=catalog_api();refresh_api.telemetry=throwing_telemetry;auto refresh_backend=create_core_backend_for_test(refresh_api);CHECK(refresh_backend->open("0000:02:00.0",error));
 Command earlier;earlier.operation=Command::Operation::apply;earlier.control=ControlId::boost;earlier.value=20;CHECK(refresh_backend->execute(earlier).ok);
 auto power_daily=native_power;power_daily["controls"]["power"]["value"]=250000;CHECK(refresh_backend->execute(catalog_command(compose_profiles({{"Board",power_daily},{"Boost",boost}}))).ok);
 catalog_refresh_throw=true;auto refresh_failed=refresh_backend->execute(undo);CHECK(!refresh_failed.ok&&board==200000&&boost_value==30&&refresh_backend->read(false).undo_available);
 CHECK(refresh_backend->execute(undo).ok&&boost_value==20&&refresh_backend->read(false).undo_available);CHECK(refresh_backend->execute(undo).ok&&boost_value==10&&!refresh_backend->read(false).undo_available);
 // A failed nested daily group retains its mutated pending/completed state.
 // A completed sibling changed afterward is compared at final verification,
 // not blindly replayed at the start of the next partial-Undo retry.
 current=40;boost_value=10;auto nested_api=catalog_api();nested_api.boost_set=refusing_boost;auto nested=create_core_backend_for_test(nested_api);CHECK(nested->open("0000:02:00.0",error));
 auto daily_pair=memory;daily_pair["controls"]["boost"]=boost["controls"]["boost"];CHECK(nested->execute(catalog_command(daily_pair)).ok&&current==80&&boost_value==30);
 catalog_boost_refuse=true;CHECK(!nested->execute(undo).ok&&current==40&&boost_value==30);catalog_boost_refuse=false;
 current=60;const auto writes_after_completed_memory=writes;auto nested_retry=nested->execute(undo);CHECK(!nested_retry.ok&&current==60&&boost_value==10&&writes==writes_after_completed_memory&&nested->read(false).undo_available);
 CHECK(nested->execute(undo).ok&&current==40&&boost_value==10&&!nested->read(false).undo_available);
 std::printf("Catalog worker integration: %d checks, %d failures\n",checks,failures);return failures?1:0;
}
