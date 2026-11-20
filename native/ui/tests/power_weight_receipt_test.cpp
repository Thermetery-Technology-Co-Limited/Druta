// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#define main weight_receipt_fixture_main
#include "power_weights_service_test.cpp"
#undef main
#include "power_weight_receipt.h"
#include "power_weight_host.h"
#include "pascal_clock_fixture.h"
#include "profile_io.h"
#include <windows.h>
#include <filesystem>
namespace {
unsigned receipt_checks=0;
void receipt_check(bool b,const char* message){++receipt_checks;if(!b)throw std::runtime_error(message);}
template<class F>void receipt_reject(F f,const char* message){bool rejected=false;try{f();}catch(const std::exception&){rejected=true;}receipt_check(rejected,message);}
// The serial keeps names unique when several Temps are made within one GetTickCount64 tick.
struct Temp {std::wstring path;Temp(){static unsigned serial=0;wchar_t base[MAX_PATH]{};receipt_check(GetTempPathW(MAX_PATH,base)!=0,"Temp directory available");path=(std::filesystem::path(base)/(L"druta-weight-receipt-test-"+std::to_wstring(GetCurrentProcessId())+L"-"+std::to_wstring(GetTickCount64())+L"-"+std::to_wstring(++serial))).wstring();receipt_check(std::filesystem::create_directory(path),"Exclusive test directory created");}~Temp(){std::error_code error;std::filesystem::remove_all(path,error);}};
void receipt_tests(){for(bool alternate:{false,true}){WeightFake fake(alternate);fake.current.registry=PowerWeightRegistrySnapshot{{L"EnableGpuFirmware",4,{0x11,0,0,0}}};auto r=make_weight_receipt(fake.current,{{0,1.25,4096},{31,0,6144}});auto derived=derive_weight_receipt(r);receipt_check(derived.plan.ok&&derived.plan.selected.size()==2&&derived.after.size()==3,"Selected semantic plan and owned names recomputed");const auto bytes=encode_weight_receipt(r);auto decoded=decode_weight_receipt(bytes);receipt_check(encode_weight_receipt(decoded)==bytes&&decoded.requests[1].expected_raw==6144,"Canonical roundtrip preserves exact raw bounds and complete evidence");receipt_check(bytes.find("nvapi_handle")==std::string::npos&&bytes.find("target")==std::string::npos,"No live API handles serialized");
 auto j=ProfileJson::parse(bytes);j["after"]=ProfileJson::array();receipt_reject([&]{decode_weight_receipt(j.dump());},"Arbitrary after-state rejected");j=ProfileJson::parse(bytes);j["requests"][0]["before_raw"]=4097;receipt_reject([&]{decode_weight_receipt(j.dump());},"Stale baseline rejected by actual planner");j=ProfileJson::parse(bytes);j["requests"].push_back(j["requests"][0]);receipt_reject([&]{decode_weight_receipt(j.dump());},"Duplicate selected relationship refused");j=ProfileJson::parse(bytes);j["windows"]["instance_id"]="PCI\\VEN_10DE&DEV_ffff&SUBSYS_12345678\\OTHER";receipt_reject([&]{decode_weight_receipt(j.dump());},"Windows target must match serialized device identity");j=ProfileJson::parse(bytes);j["layout"]["policy_stride"]=1;receipt_reject([&]{decode_weight_receipt(j.dump());},"Unsupported geometry cannot create a journal");
 r.issued_names={derived.after[1].name};r.phase="writing";r.uncertain=true;receipt_check(decode_weight_receipt(encode_weight_receipt(r)).issued_names==r.issued_names,"Partial possibly issued journal preserved");auto bad=r;bad.issued_names.push_back(L"EnableGpuFirmware");receipt_reject([&]{encode_weight_receipt(bad);},"Unrelated baseline name is never owned");bad=r;bad.issued_names.push_back(L"romOverrideFF");receipt_reject([&]{encode_weight_receipt(bad);},"Unplanned registry name cannot be journaled");
 Temp dir;receipt_check(!discover_weight_receipt(dir.path,r.identity.slot,r.uuid),"Empty store discovers no receipt");create_weight_receipt(dir.path,r);const auto active=discover_weight_receipt(dir.path,r.identity.slot,r.uuid);receipt_check(active&&active->id==r.id&&active->uncertain,"Durable active pointer discovers exact journal");auto second=make_weight_receipt(fake.current,{{0,1.5,4096}});receipt_reject([&]{create_weight_receipt(dir.path,second);},"Another active transaction cannot replace first");receipt_check(!std::filesystem::exists(power_weight_receipt_path(dir.path,second.id)),"Refused transaction has no journal or effects");auto wrong=second;wrong.phase="removed";receipt_reject([&]{clear_active_weight_receipt(dir.path,wrong);},"Another receipt cannot clear current active pointer");receipt_check(!discover_weight_receipt(dir.path,r.identity.slot,"GPU-other"),"Different UUID cannot adopt same slot journal");
 auto changed=r;changed.info.back()^=1;receipt_reject([&]{save_weight_receipt(dir.path,changed);},"Immutable complete INFO cannot be rebased in journal");receipt_check(encode_weight_receipt(read_weight_receipt(dir.path,r.id))==encode_weight_receipt(r),"Failed update retains previous durable journal");r.phase="remove_refresh_pending";r.refresh_requested=true;r.restoring=true;save_weight_receipt(dir.path,r);receipt_check(read_weight_receipt(dir.path,r.id).refresh_requested&&read_weight_receipt(dir.path,r.id).restoring,"Removal direction durably survives reload independently of failure phase");receipt_reject([&]{clear_active_weight_receipt(dir.path,r);},"Unverified active transaction cannot be cleared");r.phase="removed";r.uncertain=false;receipt_reject([&]{clear_active_weight_receipt(dir.path,r);},"Removal must be durably saved before clearing pointer");save_weight_receipt(dir.path,r);receipt_check(clear_active_weight_receipt(dir.path,r)&&!discover_weight_receipt(dir.path,r.identity.slot,r.uuid),"Verified removal clears matching pointer only");receipt_check(std::filesystem::exists(power_weight_receipt_path(dir.path,r.id)),"Removal retains durable receipt history");receipt_check(!clear_active_weight_receipt(dir.path,r),"Duplicate pointer clear is idempotent");
 receipt_reject([&]{power_weight_receipt_path(dir.path,"../outside");},"Receipt ids cannot escape direct-child directory");
 }
 WeightFake fake;fake.current.registry=PowerWeightRegistrySnapshot{{L"EnableGpuFirmware",99,std::vector<uint8_t>(600000,0x5a)}};auto large=make_weight_receipt(fake.current,{{0,1.5,4096}});const auto large_bytes=encode_weight_receipt(large);receipt_check(large_bytes.size()>profile_max_bytes&&decode_weight_receipt(large_bytes).before[0].bytes.size()==600000,"Independent receipt limit preserves unknown registry bytes above ordinary profile cap");
 Temp blocked;const auto blocked_path=power_weight_receipt_path(blocked.path,large.id);receipt_check(std::filesystem::create_directory(blocked_path),"Create a blocked journal destination");receipt_reject([&]{create_weight_receipt(blocked.path,large);},"Receipt commit failure refuses active publication");receipt_check(!discover_weight_receipt(blocked.path,large.identity.slot,large.uuid),"Failed durable journal never publishes active pointer");
 receipt_reject([&]{decode_weight_receipt("{\"id\":1,\"id\":2}");},"Duplicate JSON keys rejected");receipt_reject([&]{decode_weight_receipt(std::string(25,'[')+"0"+std::string(25,']'));},"Deep JSON rejected before schema processing");receipt_reject([&]{decode_weight_receipt(std::string(power_weight_receipt_max_bytes+1,' '));},"Oversize receipt refused independently of profile limits");fake.current.adapter.uuid[0]=0;receipt_reject([&]{make_weight_receipt(fake.current,{{0,1.5,4096}});},"Durable receipt requires UUID");
}

void clock_revision_receipt_tests() {
 WeightFake fake;
 auto& c=fake.current;
 c.adapter.nvapi_devid=c.adapter.nvml_devid=c.target.device_id=c.identity.device_id=0x1bbc;
 c.target.architecture=c.identity.architecture=4;
 c.windows->instance_id="PCI\\VEN_10DE&DEV_1BBC&SUBSYS_12345678\\CLOCK";
 c.image=test::pascal_clock_fixture();
 c.registry=PowerWeightRegistrySnapshot{{L"EnableGpuFirmware",4,{0x11,0,0,0}}};
 using D=PascalClockDomain;
 auto active=make_pascal_clock_receipt(c,{{D::gpc,0,3800,3300},{D::xbar,-1,95,105}});
 active.phase="active";
 active.refresh_requested=true;
 c.image=derive_weight_receipt(active).clock_plan.image;
 receipt_check(derive_weight_receipt(decode_weight_receipt(encode_weight_receipt(active))).clock_source_image.empty(),
               "Version 1 clock receipt has no separate pre-restart image");

 c.registry->front().name=L"ENABLEGPUFIRMWARE";
 const std::vector<PascalClockEdit> edits={{D::gpc,0,3300,3200},{D::sys,-1,90,100}};
 const auto successor=revise_pascal_clock_receipt(c,active,edits);
 const auto successor_bytes=encode_weight_receipt(successor);
 const auto decoded=decode_weight_receipt(successor_bytes);
 const auto derived=derive_weight_receipt(decoded);
 receipt_check(encode_weight_receipt(decoded)==successor_bytes&&decoded.image==active.image&&
               derived.clock_source_image==c.image&&
               derived.clock_plan.image==plan_pascal_clocks(c.image,edits).image&&decoded.clocks.size()==3,
               "Revision durably keeps original image, exact predecessor, and all current clock edits");
 auto malformed=ProfileJson::parse(successor_bytes);
 malformed["previous_requests"][0]["before_raw"]=3300;
 receipt_reject([&]{decode_weight_receipt(malformed.dump());},"Stale predecessor values are checked against the original image");
 receipt_reject([&]{revise_pascal_clock_receipt(c,active,{{D::gpc,0,3800,3200}});},"Stale slider value cannot be applied");
 auto changed=c;
 changed.image.back()^=1;
 receipt_reject([&]{revise_pascal_clock_receipt(changed,active,edits);},"Unrelated live ROM change blocks re-editing");
 changed=c;
 changed.registry->front().bytes[0]^=1;
 receipt_reject([&]{revise_pascal_clock_receipt(changed,active,edits);},"Persistent registry change blocks re-editing");
 changed=c;
 changed.windows->driver_key.back()='8';
 receipt_reject([&]{revise_pascal_clock_receipt(changed,active,edits);},"Windows rebinding blocks re-editing");
 const auto reset=revise_pascal_clock_receipt(c,active,{{D::gpc,0,3300,3800},{D::xbar,-1,105,95}});
 const auto reset_plan=derive_weight_receipt(reset);
 receipt_check(reset_plan.clock_plan.image==active.image&&reset_plan.after.size()==active.before.size(),
               "Returning all edits to original needs no temporary registry additions");

 Temp dir;
 receipt_reject([&]{create_weight_receipt(dir.path,successor);},"A clock revision cannot be replayed without its active predecessor");
 create_weight_receipt(dir.path,active);
 const auto still_active=[&]{return discover_weight_receipt(dir.path,active.identity.slot,active.uuid)->id==active.id;};
 const auto lock_path=power_weight_operation_lock_path(dir.path,active.id);
 const auto lock=CreateFileW(lock_path.c_str(),GENERIC_READ|GENERIC_WRITE,0,nullptr,OPEN_ALWAYS,
                             FILE_ATTRIBUTE_NORMAL|FILE_FLAG_DELETE_ON_CLOSE,nullptr);
 receipt_check(lock!=INVALID_HANDLE_VALUE,"Simulated active helper lock acquired");
 receipt_reject([&]{replace_pascal_clock_receipt(dir.path,active,successor);},"Cannot replace while the prior helper is operating");
 CloseHandle(lock);
 receipt_check(still_active(),"Helper conflict leaves previous receipt active");
 auto stale=active;
 stale.message="Current durable state changed";
 save_weight_receipt(dir.path,stale);
 receipt_reject([&]{replace_pascal_clock_receipt(dir.path,active,successor);},"Same-ID journal change invalidates complete CAS");
 receipt_check(still_active(),"Stale revision leaves previous receipt active");

 const auto blocked_path=power_weight_receipt_path(dir.path,successor.id);
 receipt_check(std::filesystem::create_directory(blocked_path),"Create a blocked successor journal destination");
 receipt_reject([&]{replace_pascal_clock_receipt(dir.path,stale,successor);},"Cannot publish a successor whose journal failed to commit");
 receipt_check(still_active(),"Failed successor journal leaves previous receipt active");
 std::filesystem::remove(blocked_path);
 replace_pascal_clock_receipt(dir.path,stale,successor);
 receipt_check(discover_weight_receipt(dir.path,active.identity.slot,active.uuid)->id==successor.id&&
               read_weight_receipt(dir.path,active.id).message==stale.message,
               "Replacement publishes the new receipt while retaining the complete previous journal");
 receipt_reject([&]{replace_pascal_clock_receipt(dir.path,stale,reset);},"An older predecessor cannot replace the current revision");
 const auto slot_clocks=active_clock_receipts(dir.path,successor.identity.slot);
 receipt_check(slot_clocks.size()==1&&slot_clocks.front().id==successor.id,"Rescue discovery followed a retired receipt");
 receipt_check(active_clock_receipts(dir.path).size()==1&&active_clock_receipts(dir.path,"0000:ff:00.0").empty(),
               "Rescue discovery did not filter PCI slots");
 receipt_reject([&]{save_weight_receipt(dir.path,stale);},"An older helper cannot write after replacement");
 auto started=successor;started.phase="writing";
 save_weight_receipt(dir.path,started);
 receipt_reject([&]{abandon_prepared_clock_receipt(dir.path,successor);},"A stale cancel cannot retire a running trial");
 save_weight_receipt(dir.path,successor);
 abandon_prepared_clock_receipt(dir.path,successor);
 receipt_check(still_active()&&read_weight_receipt(dir.path,active.id).message==stale.message,
               "Canceling a revision returns to the verified predecessor");
 Temp first_dir;
 auto unused=active;unused.phase="prepared";unused.refresh_requested=false;
 create_weight_receipt(first_dir.path,unused);
 abandon_prepared_clock_receipt(first_dir.path,unused);
 receipt_check(!discover_weight_receipt(first_dir.path,unused.identity.slot,unused.uuid),
               "Canceling first Apply releases the sliders without a restart");

 // The supervised helper may lose P0 before its first hardware effect. Simulate
 // the actual timing receipt and the same operation lock held by a concurrent worker.
 Temp timing_dir;
 c.image=test::pascal_timing_fixture();
 c.timing_sample=PascalTimingSample{3504,0x2966c296,0x38aa};
 const auto timing=make_pascal_clock_receipt(c,{{D::tfaw,0,28,29,c.timing_sample}});
 create_weight_receipt(timing_dir.path,timing);
 with_clock_operation_lock(timing_dir.path,timing.id,[&]{
  receipt_reject([&]{cancel_unused_clock_preparation(timing,timing_dir.path);},"A losing launcher cannot cancel another worker's timing edit");
 });
 receipt_check(discover_weight_receipt(timing_dir.path,timing.identity.slot,timing.uuid).has_value(),
               "The writer keeps its timing receipt on contention");
 auto writing=timing;writing.phase="writing";
 save_weight_receipt(timing_dir.path,writing);
 auto at_risk=writing;at_risk.uncertain=true;
 at_risk.issued_names.push_back(derive_weight_receipt(writing).after.back().name);
 save_weight_receipt(timing_dir.path,at_risk);
 receipt_reject([&]{cancel_unused_clock_preparation(timing,timing_dir.path);},"An issued timing effect keeps the recovery journal");
 save_weight_receipt(timing_dir.path,writing);
 cancel_unused_clock_preparation(timing,timing_dir.path);
 cancel_unused_clock_preparation(timing,timing_dir.path);
 receipt_check(!discover_weight_receipt(timing_dir.path,timing.identity.slot,timing.uuid),
               "An unused first timing trial can be retired twice without a driver restart");

 // Upgrade a journal from the release that sampled just CONFIG1 and CONFIG3.
 const auto full=test::pascal_full_timing_sample();
 const PascalTimingSample old_sample{full.memory_mhz,full.config1,full.config3};
 c.image=test::pascal_timing_fixture();
 c.timing_sample=full;
 const int faw=pascal_timing_value(full,D::tfaw);
 auto old=make_pascal_clock_receipt(c,{{D::tfaw,0,faw,faw+1,old_sample}});
 const auto old_doc=ProfileJson::parse(encode_weight_receipt(old));
 receipt_check(old_doc["requests"][0]["p0"].size()==3&&old_doc["version"]==1,
               "Old tFAW receipt preserves its original serialized evidence");
 old=decode_weight_receipt(old_doc.dump());
 old.phase="active";
 c.image=derive_weight_receipt(old).clock_plan.image;
 const auto previous_p0=with_pascal_timing(full,D::tfaw,faw+1);
 c.timing_sample=previous_p0;
 const int rc=pascal_timing_value(full,D::trc),rfc=pascal_timing_value(full,D::trfc);
 auto revised=revise_pascal_clock_receipt(c,old,{{D::trc,0,rc,rc+1,previous_p0},{D::trfc,0,rfc,rfc+2,previous_p0}});
 const auto revised_doc=ProfileJson::parse(encode_weight_receipt(revised));
 revised=decode_weight_receipt(revised_doc.dump());
 const auto target=with_pascal_timing(with_pascal_timing(previous_p0,D::trc,rc+1),D::trfc,rfc+2);
 receipt_check(receipt_timing_sample(revised,PascalTimingPhase::original)==full&&
               receipt_timing_sample(revised,PascalTimingPhase::previous)==previous_p0&&
               receipt_timing_sample(revised,PascalTimingPhase::applied)==target,
               "Two additional fields sharing CONFIG0 keep the original timing and preceding tFAW proof");
 auto missing=revised_doc;
 missing["requests"][1]["p0"].erase("config0");
 receipt_reject([&]{decode_weight_receipt(missing.dump());},"An edit requires its own physical word");
 auto foreign=revised_doc;
 foreign["requests"][1]["domain"]="tcl";
 receipt_reject([&]{decode_weight_receipt(foreign.dump());},"A journal cannot enable a read-only timing field");
 auto extra=revised_doc;
 extra["requests"][1]["p0"]["unknown"]=1234;
 receipt_reject([&]{decode_weight_receipt(extra.dump());},"Unknown timing controller words are rejected");
 revised.phase="active";
 c.image=derive_weight_receipt(revised).clock_plan.image;
 c.timing_sample=target;
 auto restored=revise_pascal_clock_receipt(c,revised,{{D::trc,0,rc+1,rc,target}});
 receipt_check(restored.clocks.size()==2&&receipt_timing_sample(restored,PascalTimingPhase::applied)==
               with_pascal_timing(previous_p0,D::trfc,rfc+2),"One field can return to stock while its neighbor and tFAW stay selected");
 restored.phase="active";
 c.image=derive_weight_receipt(restored).clock_plan.image;
 c.timing_sample=receipt_timing_sample(restored,PascalTimingPhase::applied);
 auto stock=revise_pascal_clock_receipt(c,restored,{{D::tfaw,0,faw+1,faw,c.timing_sample},
                                                    {D::trfc,0,rfc+2,rfc,c.timing_sample}});
 receipt_check(derive_weight_receipt(stock).clock_plan.image==old.image&&receipt_timing_sample(stock,PascalTimingPhase::applied)==full,
               "Returning independent timing overrides to stock restores the complete original evidence");

}
}
int main(){receipt_tests();clock_revision_receipt_tests();std::printf("Power weight receipt: %u checks\n",receipt_checks);return 0;}
