// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Reuse the established injected CoreApi fixtures, without running their
// separate test entry point. No DLL, device, files or helper are opened here.
#define main established_core_fixture_entry
#include "core_client_test.cpp"
#undef main
#include "profile_catalog.h"
#include "curve_profile.h"
#include "fan_profile.h"
#include "python_full_profile.h"
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
static dc_fan_state catalog_fans{};static int catalog_fan_writes=0;static bool fan_partial=false,fan_mutate_after=false,fan_false_success=false,fan_partial_success=false,fan_change_identity=false;
static dc_status DC_CALL individual_fan_read(dc_gpu*,dc_fan_state* out){*out=catalog_fans;return DC_OK;}
static dc_status DC_CALL individual_fan_restore(dc_gpu*,const dc_fan_snapshot* req,dc_outcome* out){
 // Match the real ABI's same-source positional-ID contract.
 if(req->count!=catalog_fans.count||std::strcmp(req->source,catalog_fans.source)){out->ok=0;return DC_OK;}
 for(uint32_t i=0;i<req->count;++i)if(req->fans[i].id!=catalog_fans.fans[i].id){out->ok=0;return DC_OK;}
 ++catalog_fan_writes;if(fan_false_success){fan_false_success=false;out->ok=1;return DC_OK;}
 for(uint32_t i=0;i<req->count;++i){auto& row=catalog_fans.fans[i];row.manual=req->fans[i].manual;row.policy=req->fans[i].policy;if(row.manual)row.level=req->fans[i].level;
  if(fan_partial_success){fan_partial_success=false;out->ok=1;return DC_OK;}}
 if(fan_change_identity){fan_change_identity=false;changed_id=true;}
 if(fan_partial){fan_partial=false; // Only the first fan changed; preserve the other prior request.
  catalog_fans.fans[1].manual=0;catalog_fans.fans[1].policy=0;out->ok=0;return DC_OK;}
 out->ok=1;return DC_OK;}
static dc_status DC_CALL fan_interference_setter(dc_gpu* gpu,uint32_t type,double value,int32_t preserve,dc_outcome* out){const auto result=setter(gpu,type,value,preserve,out);if(fan_mutate_after)catalog_fans.fans[0].level=91;return result;}
static void reset_catalog_fans(){catalog_fans={};catalog_fans.size=sizeof(catalog_fans);std::strcpy(catalog_fans.source,"nvapi_client");catalog_fans.count=2;
 for(unsigned i=0;i<2;++i){auto& f=catalog_fans.fans[i];f.id=i?19:7;f.min=20;f.max=100;f.default_policy=0;f.level=i?30:40;f.manual=i?0:1;f.policy=i?0:1;}}
#if defined(_MSC_VER)
__declspec(noinline)
#endif
static void fan_catalog_tests(){std::string error;reset_catalog_fans();catalog_fan_writes=0;auto api=catalog_api();api.fan_state=individual_fan_read;api.fan_restore=individual_fan_restore;api.set_offset=fan_interference_setter;
 auto client=create_core_backend_for_test(api);CHECK(client->open("0000:02:00.0",error));dc_gpu_identity ident{};ident.nvml_ok=1;std::strcpy(ident.slot,"0000:02:00.0");std::strcpy(ident.uuid,"GPU-A");
 auto desired=catalog_fans;desired.fans[0].level=60;desired.fans[1].manual=1;desired.fans[1].policy=1;desired.fans[1].level=70;
 auto profile=capture_fan_profile(ident,desired);auto before=catalog_fan_writes;CHECK(client->execute(catalog_command(profile,true)).ok&&catalog_fan_writes==before);
 ProfileJson python={{"schema",2},{"scope","fan"},{"saved_at","2026-11-13"},{"saved_ts",1},{"device",{{"name","alternate"},{"vbios",nullptr},{"driver",nullptr},{"uuid","GPU-A"},{"slot","0000:02:00.0"}}},{"incomplete",ProfileJson::array()},{"fan_control_state",profile["fan_control_state"]}};
 CHECK(client->execute(catalog_command(python,true)).ok&&catalog_fan_writes==before);Command python_undo;python_undo.operation=Command::Operation::undo;
 CHECK(client->execute(catalog_command(python)).ok&&catalog_fans.fans[0].level==60);CHECK(client->execute(python_undo).ok&&catalog_fans.fans[0].level==40);before=catalog_fan_writes;
 wchar_t dir[MAX_PATH]{},path[MAX_PATH]{};CHECK(GetTempPathW(MAX_PATH,dir)&&GetTempFileNameW(dir,L"fan",0,path));DeleteFileW(path);
 Command save;save.operation=Command::Operation::save_fan_profile;save.profile_path=path;CHECK(client->execute(save).ok&&catalog_fan_writes==before);
 CHECK(parse_fan_profile(read_profile(path)).requests.fans[0].level==40);DeleteFileW(path);
 auto foreign=profile;foreign["target"]["uuid"]="GPU-other";CHECK(!client->execute(catalog_command(foreign)).ok&&catalog_fan_writes==before);
 catalog_fans.fans[1].id=20;CHECK(!client->execute(catalog_command(profile)).ok&&catalog_fan_writes==before);catalog_fans.fans[1].id=19;
 Command undo;undo.operation=Command::Operation::undo;fan_partial=true;auto partial=client->execute(catalog_command(profile));CHECK(!partial.ok&&partial.may_have_changed&&catalog_fans.fans[0].level==60&&client->read(false).undo_available);
 CHECK(client->execute(undo).ok&&catalog_fans.fans[0].level==40&&!catalog_fans.fans[1].manual);
 current=40;mode=2;auto both=compose_profiles({{"Fan",profile},{"Memory",catalog_daily("memory",80)}});auto failed=client->execute(catalog_command(both));
 CHECK(!failed.ok&&catalog_fans.fans[0].level==60&&client->read(false).undo_available);mode=0;CHECK(client->execute(undo).ok&&catalog_fans.fans[0].level==40);
 CHECK(client->execute(catalog_command(profile)).ok);catalog_fans.fans[0].level=80;const auto writes=catalog_fan_writes;
 CHECK(!client->execute(undo).ok&&catalog_fan_writes==writes&&client->read(false).undo_available);catalog_fans.fans[0].level=60;CHECK(client->execute(undo).ok&&catalog_fans.fans[0].level==40);
 current=40;fan_mutate_after=true;auto changed=client->execute(catalog_command(both));CHECK(!changed.ok&&changed.may_have_changed&&client->read(false).undo_available&&catalog_fans.fans[0].level==91);
 fan_mutate_after=false;catalog_fans.fans[0].level=60;CHECK(client->execute(undo).ok&&catalog_fans.fans[0].level==40&&current==40);
 // Rebase saved requests by ID before calling the positional ABI. Final
 // aggregate verification must also accept the same IDs in a new order.
 CHECK(client->execute(catalog_command(profile)).ok);std::swap(catalog_fans.fans[0],catalog_fans.fans[1]);
 CHECK(client->execute(undo).ok&&catalog_fans.fans[0].id==19&&!catalog_fans.fans[0].manual&&catalog_fans.fans[1].id==7&&catalog_fans.fans[1].level==40);
 std::swap(catalog_fans.fans[0],catalog_fans.fans[1]);CHECK(!client->read(false).undo_available);
 // A success flag without full restoration must retain the original receipt.
 CHECK(client->execute(catalog_command(profile)).ok);fan_false_success=true;
 CHECK(!client->execute(undo).ok&&catalog_fans.fans[0].level==60&&client->read(false).undo_available);
 CHECK(client->execute(undo).ok&&catalog_fans.fans[0].level==40&&!catalog_fans.fans[1].manual);
 CHECK(client->execute(catalog_command(profile)).ok);fan_partial_success=true;
 CHECK(!client->execute(undo).ok&&catalog_fans.fans[0].level==40&&catalog_fans.fans[1].manual&&client->read(false).undo_available);
 CHECK(client->execute(undo).ok&&!catalog_fans.fans[1].manual&&!client->read(false).undo_available);
 // Restore can finish while identity changes: retain Undo, then verify the
 // original state without replay once the original pairing is available.
 CHECK(client->execute(catalog_command(profile)).ok);fan_change_identity=true;
 CHECK(!client->execute(undo).ok&&changed_id&&catalog_fans.fans[0].level==40);
 changed_id=false;CHECK(client->read(false).undo_available);const auto restored_writes=catalog_fan_writes;
 CHECK(client->execute(undo).ok&&catalog_fan_writes==restored_writes&&!client->read(false).undo_available);
 client->close();}
static bool full_curve_drift=false;
static bool full_current_coupling=false;static int full_generic_peer=100;
static dc_status DC_CALL full_memory_set(dc_gpu* g,uint32_t t,double v,int32_t p,dc_outcome* o){const auto status=setter(g,t,v,p,o);if(full_curve_drift&&t==2)++voltage_shift;return status;}
static dc_status DC_CALL full_policies(dc_gpu* g,dc_cl_confirm_fn c,void* cx,dc_cl_policy_inputs_fn in,void* ix,dc_policy_row* rows,uint32_t cap,uint32_t* n,dc_error* e){const auto status=policies(g,c,cx,in,ix,rows,cap,n,e);if(status==DC_OK){rows[2]={};rows[2].policy=8;rows[2].type=13;rows[2].channel=15;rows[2].unit=-1;rows[2].requested=77;rows[2].maximum=100;*n=3;if(full_current_coupling){rows[1].named_current=1;rows[3]={};rows[3].policy=2;rows[3].channel=3;rows[3].unit=0;rows[3].requested=full_generic_peer;rows[3].maximum=500;rows[3].writable=rows[3].consistent=1;*n=4;}}return status;}
static dc_status DC_CALL full_policy_set(dc_gpu* g,dc_cl_confirm_fn c,void* cx,dc_cl_policy_inputs_fn in,void* ix,int64_t id,int32_t whole,int64_t value,int32_t pin,int32_t xoc,int32_t* ok,char* text,size_t cap,size_t* needed){if(full_current_coupling&&id==2){++policy_writes;full_generic_peer=static_cast<int>(value);*ok=1;*needed=1;return DC_OK;}const auto status=policy_set(g,c,cx,in,ix,id,whole,value,pin,xoc,ok,text,cap,needed);if(full_current_coupling&&status==DC_OK&&*ok)full_generic_peer=260;return status;}
static ProfileJson full_document(){return {{"schema",2},{"saved_at","2026-11-13"},{"saved_ts",1},{"device",{{"name","Alternate adapter"},{"vbios",nullptr},{"driver","unlisted"},{"uuid","GPU-A"},{"slot","0000:02:00.0"},{"mem_type_id",nullptr}}},{"incomplete",ProfileJson::array()},{"core_off_mhz",12},{"mem_off_units",80},{"mem_off_true_mhz",5},{"mem_off_scale",16},{"volt_boost_pct",30},{"vf_deltas",{{"3",1000},{"4",2000}}}};}
#if defined(_MSC_VER)
__declspec(noinline)
#endif
static void full_tune_tests(){
 std::string error;Command undo;undo.operation=Command::Operation::undo;current=40;boost_value=10;delta=123;delta2=456;mode=0;voltage_shift=0;core_value=30;board=200000;peer=100;pin_present=true;pinned_value=100;
 auto a=catalog_api();a.set_offset=full_memory_set;a.policy_read=full_policies;a.policy_set=full_policy_set;auto client=create_core_backend_for_test(a);CHECK(client->open("0000:02:00.0",error));
 auto doc=full_document();auto before=writes,before_curve=curve_writes;CHECK(client->execute(catalog_command(doc,true)).ok&&writes==before&&curve_writes==before_curve);
 auto applied=client->execute(catalog_command(doc));if(!applied.ok)std::fprintf(stderr,"Full tune: %s\n",applied.message.c_str());CHECK(applied.ok&&current==80&&boost_value==30&&delta==1000&&delta2==2000&&core_value==30);CHECK(client->execute(undo).ok&&current==40&&boost_value==10&&delta==123&&delta2==456);
 // No historical geometry is claimed: complete current indices are required,
 // then the preflight binding stays fixed through earlier component writes.
 auto invalid=doc;invalid["vf_deltas"]["5"]=3;before=writes;CHECK(!client->execute(catalog_command(invalid)).ok&&writes==before);
 full_curve_drift=true;before_curve=curve_writes;auto drift=client->execute(catalog_command(doc));CHECK(!drift.ok&&drift.may_have_changed&&curve_writes==before_curve);full_curve_drift=false;voltage_shift=0;CHECK(client->execute(undo).ok&&current==40&&boost_value==10);
 // Unhandled controller state remains visible, never silently omitted. Only
 // an explicit selection may omit it, while malformed source still refuses.
 auto with_i2c=doc;with_i2c["i2c"]={{"profile","RTX 2080 Ti - NVVDD (MP2888A)"},{"sha256","4a3979d6500eca53c3f37e7ccf7f0a1ad2ea71ee819a8e26c11a535c912470f7"},{"port",1},{"addr7",32},{"rail","NVVDD"},{"display_name","offset"},{"offset_mv",12.5}};before=writes;CHECK(!client->execute(catalog_command(with_i2c)).ok&&writes==before);auto selected=select_profile_targets(with_i2c,{"clock:memory"});CHECK(client->execute(catalog_command(selected)).ok&&current==80&&boost_value==10);CHECK(client->execute(undo).ok&&current==40);
 auto malformed=selected;malformed["source"]["msvdd_offsets_mv"]={{"5",0.0001}};before=writes;CHECK(!client->execute(catalog_command(malformed)).ok&&writes==before);
 // Whole-source identities and canonical overlaps are enforced on imports.
 auto foreign=doc;foreign["device"]["uuid"]="GPU-foreign";CHECK(!client->execute(catalog_command(foreign)).ok&&writes==before);
 auto duplicate=ProfileJson{{"format","druta-native-combined"},{"version",1},{"components",ProfileJson::array({{{"name","Full"},{"state",doc}},{{"name","Memory"},{"state",catalog_daily("memory",80)}}})}};CHECK(!client->execute(catalog_command(duplicate)).ok&&writes==before);
 // Unpinned saved rows keep a newly derived driver request. An unrelated
 // read-only row neither becomes a setter nor disables the saved controls.
 auto power=doc;power["power_limit_mw"]=250000;power["power_policies"]={{"1",{{"value",150},{"type",0},{"channel",0}}}};power=select_profile_targets(power,{"power:*"});
 CHECK(client->execute(catalog_command(power,true)).ok);auto pr=client->execute(catalog_command(power));if(!pr.ok)std::fprintf(stderr,"Full power: %s\n",pr.message.c_str());CHECK(pr.ok&&board==250000&&peer==200&&!pin_present);CHECK(client->execute(undo).ok&&board==200000&&peer==100&&pin_present&&pinned_value==100);
 auto pinned=python_full_source(power);pinned["power_policy_pins"]={{"1",{{"value",150},{"type",0},{"channel",0}}}};pinned=select_profile_targets(pinned,{"power:*"});CHECK(client->execute(catalog_command(pinned)).ok&&peer==150&&pin_present&&pinned_value==150);CHECK(client->execute(undo).ok&&board==200000&&peer==100&&pinned_value==100);
 auto badpin=pinned;badpin["source"]["power_policy_pins"]["1"]["channel"]=9;const auto before_power=power_writes;CHECK(!client->execute(catalog_command(badpin)).ok&&power_writes==before_power);
 // A possibly dispatched board failure retains original coupled recovery.
 power_throw=true;auto failed=client->execute(catalog_command(power));CHECK(!failed.ok&&failed.may_have_changed&&client->read(false).undo_available);power_throw=false;CHECK(client->execute(undo).ok&&board==200000&&peer==100&&pin_present);
 full_current_coupling=true;full_generic_peer=100;auto coupled=python_full_source(power);coupled["current_limits_ma"]={{"1",150}};coupled["power_policies"]["2"]={{"value",190},{"channel",3},{"type",0}};coupled=select_profile_targets(coupled,{"power:*"});
 auto cr=client->execute(catalog_command(coupled));if(!cr.ok)std::fprintf(stderr,"Full coupled current: %s\n",cr.message.c_str());CHECK(cr.ok&&board==250000&&peer==150&&full_generic_peer==260&&!pin_present);CHECK(client->execute(undo).ok&&board==200000&&peer==100&&full_generic_peer==100&&pin_present);full_current_coupling=false;
 client->close();
}
int main(){std::string error;Command undo;undo.operation=Command::Operation::undo;
 current=40;boost_value=10;writes=boost_writes=0;mode=0;auto a=catalog_api();
 auto backend=create_core_backend_for_test(a);CHECK(backend->open("0000:02:00.0",error));
 auto memory=catalog_daily("memory",80),boost=catalog_daily("boost",30);auto composed=compose_profiles({{"Memory",memory},{"Boost",boost}});
 CHECK(backend->execute(catalog_command(composed,true)).ok&&writes==0&&boost_writes==0&&!backend->read(false).undo_available);
 CHECK(backend->execute(catalog_command(composed)).ok&&current==80&&boost_value==30);CHECK(backend->read(false).undo_available);
 CHECK(backend->execute(undo).ok&&current==40&&boost_value==10&&!backend->read(false).undo_available);
 // Legacy timing is structurally stageable, but cannot be preflighted with a memory offset.
 auto legacy_timing=ProfileJson{{"fields",{{"RC",20}}}};const auto before_legacy=writes;
 CHECK(!backend->execute(catalog_command(compose_profiles({{"Memory",memory},{"Legacy timing",legacy_timing}}),true)).ok&&writes==before_legacy);
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
 fan_catalog_tests();
 full_tune_tests();
 std::printf("Catalog worker integration: %d checks, %d failures\n",checks,failures);return failures?1:0;
}
