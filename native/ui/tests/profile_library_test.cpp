// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "profile_library.h"
#include "quick_profiles.h"
#include <windows.h>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <set>
#include <stdexcept>
using namespace druta::ui;
namespace {
namespace fs=std::filesystem;
unsigned checks=0,failures=0;
#define CHECK(x) do{++checks;if(!(x)){++failures;std::fprintf(stderr,"Profile library line %d: %s\n",__LINE__,#x);}}while(false)
template<class F>void reject(F f){bool refused=false;try{f();}catch(const std::exception&){refused=true;}CHECK(refused);}
// The serial keeps names unique when several Temps are made within one GetTickCount64 tick.
struct Temp {fs::path path;Temp(){static unsigned serial=0;wchar_t base[MAX_PATH]{};if(!GetTempPathW(MAX_PATH,base))throw std::runtime_error("Temp root unavailable");path=fs::path(base)/(L"druta-profile-library-test-"+std::to_wstring(GetCurrentProcessId())+L"-"+std::to_wstring(GetTickCount64())+L"-"+std::to_wstring(++serial));if(!fs::create_directory(path))throw std::runtime_error("Cannot create exclusive test directory");}~Temp(){std::error_code ec;fs::remove_all(path,ec);}};
void raw(const fs::path& path,const std::string& content){fs::create_directories(path.parent_path());std::ofstream out(path,std::ios::binary);out<<content;if(!out)throw std::runtime_error("Test fixture write failed");}
void stamp(const fs::path& path,uint64_t ticks){HANDLE file=CreateFileW(path.c_str(),FILE_WRITE_ATTRIBUTES,0,nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr);CHECK(file!=INVALID_HANDLE_VALUE);FILETIME time{static_cast<DWORD>(ticks),static_cast<DWORD>(ticks>>32)};CHECK(SetFileTime(file,nullptr,nullptr,&time)!=0);CloseHandle(file);}
void discovery_and_listing(){Temp temp;const auto source=temp.path/L"source",binary=source/L"native"/L"build",local=temp.path/L"local";raw(source/L"pyproject.toml","");fs::create_directories(source/L"src"/L"druta");fs::create_directories(source/L"profiles");fs::create_directories(binary/L"profiles");fs::create_directories(local/L"Thermetery"/L"Druta"/L"profiles");const auto native=local/L"Druta"/L"profiles";
 const auto dirs=discover_profile_directories({(binary/L"Druta.exe").wstring(),local.wstring()});CHECK(dirs.size()==4&&dirs[0].writable&&dirs[0].path==native.wstring());CHECK(!fs::exists(native));CHECK(list_profile_library(dirs).empty()&&!fs::exists(native));
 raw(source/L"profiles"/L"same.json",R"({"schema":2,"scope":"tuning_action","saved_ts":4000000000.25,"saved_at":"newer"})");raw(binary/L"profiles"/L"same.json",R"({"saved_ts":4000000000.125,"saved_at":"older"})");raw(local/L"Thermetery"/L"Druta"/L"profiles"/L"autosave-python.json",R"({"saved_ts":4000000000.0})");
 raw(source/L"profiles"/L"broken.json","{not json");raw(source/L"profiles"/L"oversize.json",std::string(profile_max_bytes+1,' '));raw(source/L"profiles"/L"array.json","[]");raw(source/L"profiles"/L"ignored.txt","{}");fs::create_directories(source/L"profiles"/L"directory.json");
 auto entries=list_profile_library(dirs);CHECK(entries.size()==6&&entries[0].source=="Python source"&&entries[0].saved_at=="newer"&&entries[1].source=="Portable"&&entries[2].autosave&&!entries[2].library_owned);CHECK(entries[0].id!=entries[1].id&&entries[0].name==entries[1].name);unsigned errors=0;for(const auto& entry:entries)if(!entry.error.empty()){++errors;CHECK(!entry.document);}CHECK(errors==3);CHECK(entries[0].document&&entries[0].document->at("schema")==2);
 CHECK(newest_profile_autosave(entries)&&newest_profile_autosave(entries)->name=="autosave-python");std::vector<ProfileLibraryEntry> empty;CHECK(!newest_profile_autosave(empty));ProfileLibraryEntry broken;broken.name="autosave-broken";broken.autosave=true;broken.error="bad JSON";auto newest=entries;newest.insert(newest.begin(),broken);CHECK(newest_profile_autosave(newest)==&newest.front()&&newest_profile_autosave(newest)->error=="bad JSON");
 auto doubled=dirs;doubled.push_back(dirs[1]);CHECK(list_profile_library(doubled).size()==entries.size());
 // No metadata timestamps: precise mtime beats alphabetical filename ordering.
 Temp order;raw(order.path/L"Z-old.json",R"({"saved_ts":"not numeric"})");raw(order.path/L"A-new.json","{}");constexpr uint64_t base=116444736000000000ull+1700000000ull*10000000ull;stamp(order.path/L"Z-old.json",base+10000);stamp(order.path/L"A-new.json",base+20000);auto ordered=list_profile_library({{order.path.wstring(),"mtime",false}});CHECK(ordered.size()==2&&ordered[0].name=="A-new"&&ordered[0].timestamp>ordered[1].timestamp);CHECK(ordered[0].saved_at.size()==19&&ordered[0].saved_at[4]=='-'&&ordered[0].saved_at[10]==' '&&!ordered[0].document->contains("saved_at"));
 // Resolving a nonexistent executable root never searches the current directory.
 const auto elsewhere=temp.path/L"other";fs::create_directories(elsewhere);auto minimal=discover_profile_directories({(elsewhere/L"Druta.exe").wstring(),(temp.path/L"empty-local").wstring()});CHECK(minimal.size()==1&&minimal[0].source=="Native");CHECK(!fs::exists(temp.path/L"empty-local"));
}
void saving_and_history(){Temp temp;ProfileLibraryDirectory native{(temp.path/L"native").wstring(),"Native",true},external{(temp.path/L"external").wstring(),"Python",false};const ProfileJson document={{"format","druta-native-daily"},{"version",1},{"target",{{"slot","0000:39:00.0"},{"uuid","GPU-library"}}},{"controls",{{"boost",10}}}};
 const auto prepared=profile_library_named_path(native,"My tune / test");CHECK(fs::is_directory(native.path)&&!fs::exists(prepared));const auto named=save_library_profile(native,"My tune / test",document);CHECK(named==prepared);CHECK(fs::path(named).filename()==L"My-tune-test.json"&&read_profile(named)==document);auto changed=document;changed["controls"]["boost"]=20;CHECK(save_library_profile(native,"My tune / test",changed)==named&&read_profile(named)==changed);reject([&]{save_library_profile(external,"no write",document);});CHECK(!fs::exists(external.path));reject([&]{save_library_profile(native,"autosave-native-pretend",document);});
 // Failed serialization must preserve the old named document.
 reject([&]{save_library_profile(native,"My tune / test",ProfileJson::array());});CHECK(read_profile(named)==changed);
 const auto backup=save_library_timing_backup(native,document);const auto backup_again=save_library_timing_backup(native,changed);
 CHECK(backup!=backup_again&&fs::path(backup).parent_path()==fs::path(native.path)/L"timing-backups"&&read_profile(backup)==document&&read_profile(backup_again)==changed);
 reject([&]{save_library_timing_backup(external,document);});reject([&]{save_library_timing_backup(native,ProfileJson::array());});
 raw(fs::path(native.path)/L"autosave-python-old.json","{}");raw(fs::path(external.path)/L"autosave-native-foreign-0000000000000000-00000000-00000000.json","{}");
 std::set<std::wstring> paths;std::wstring last;for(unsigned i=0;i<24;++i){last=save_library_autosave(native,"boost",document);CHECK(paths.insert(last).second);}CHECK(paths.size()==24&&fs::exists(last));auto entries=list_profile_library({native,external});unsigned own=0;for(const auto& e:entries)if(e.library_owned){++own;CHECK(e.autosave&&!e.saved_at.empty()&&e.document&&*e.document==document&&!e.document->contains("saved_ts"));}CHECK(own==20&&fs::exists(named)&&fs::exists(fs::path(native.path)/L"autosave-python-old.json")&&fs::exists(fs::path(external.path)/L"autosave-native-foreign-0000000000000000-00000000-00000000.json"));
 const auto before=list_profile_library({native}).size();reject([&]{save_library_autosave(native,"bad",ProfileJson::array());});CHECK(list_profile_library({native}).size()==before);
 CHECK(fs::exists(backup)&&fs::exists(backup_again)&&fs::directory_iterator(fs::path(native.path)/L"timing-backups")!=fs::directory_iterator{});
 CHECK(std::none_of(entries.begin(),entries.end(),[&](const auto& entry){return entry.path==backup||entry.path==backup_again;}));
 // A file occupying the chosen destination directory fails without touching its bytes.
 const auto blocked=temp.path/L"blocked";raw(blocked,"keep");ProfileLibraryDirectory bad{blocked.wstring(),"Native",true};reject([&]{save_library_profile(bad,"name",document);});reject([&]{save_library_autosave(bad,"action",document);});std::ifstream check(blocked);std::string value;check>>value;CHECK(value=="keep");
 auto missing=list_profile_library({{(temp.path/L"missing").wstring(),"Missing",false}});CHECK(missing.empty()&&!fs::exists(temp.path/L"missing"));auto unavailable=list_profile_library({{blocked.wstring(),"Blocked",false}});CHECK(unavailable.size()==1&&!unavailable[0].error.empty()&&!unavailable[0].document);
}
void toolbar_saving() {
    Temp temp;
    const ProfileLibraryDirectory native{(temp.path / L"profiles").wstring(), "Native", true};
    const auto settings = (temp.path / L"slots.json").wstring();
    const std::string gpu = "GPU-test", pci = "0000:01:00.0";
    const ProfileJson daily = {{"format","druta-native-daily"},{"version",1},
        {"target",{{"uuid",gpu},{"slot",pci}}},
        {"controls",{{"core",{{"value",30},{"unit","MHz"}}},{"boost",{{"value",10},{"unit","%"}}}}}};
    ToolbarProfileSaveRequest request;
    request.name = "Daily tune"; request.slot = 3; request.expected_slot_id = "";
    const auto save = [&](const ProfileJson& doc) { return save_toolbar_profile(native, settings, request, doc, gpu, pci); };
    const auto first = save(daily);
    CHECK(first.entry.name == "Daily-tune" && first.entry.description.valid && first.entry.document == daily);
    CHECK(first.settings_error.empty() && first.slots && first.slots->ids[3] == first.entry.id);
    const auto saved_slot = load_quick_profiles(settings, {});
    CHECK(saved_slot.save_scope == ProfileScope::daily && saved_slot.save_slot == 3);
    CHECK(list_profile_library({native}).front().id == first.entry.id && read_profile(first.entry.path) == daily);
    request.slot.reset(); request.expected_slot_id.reset();
    request.name = "DAILY / tune";  // Same Windows filename after normalization.
    reject([&]{save(daily);});
    request.name = "Daily tune";
    request.replace_document = daily;
    reject([&]{save(daily);});
    request.replace_id = first.entry.id + "-other";
    reject([&]{save(daily);});
    request.replace_id = first.entry.id;
    auto fresh = daily;
    fresh["controls"]["core"]["value"] = 45;
    auto fewer = fresh;
    fewer["controls"].erase("boost");
    reject([&]{save(fewer);});
    auto other_gpu = fresh;
    other_gpu["target"]["uuid"] = "GPU-other";
    reject([&]{save(other_gpu);});
    CHECK(read_profile(first.entry.path) == daily);
    CHECK(save(fresh).entry.document == fresh);
    CHECK(!load_quick_profiles(settings, {}).save_slot);
    reject([&]{save(daily);});  // Another window already updated the reviewed document.
    const auto renamed = fs::path(native.path) / L"moved.json";
    fs::rename(first.entry.path, renamed);
    reject([&]{save(daily);});
    CHECK(!fs::exists(first.entry.path) && read_profile(renamed.wstring()) == fresh);
    request = {};
    request.name = "Smaller"; request.slot = 3; request.expected_slot_id = "";
    reject([&]{save(fewer);});  // Slot now belongs to the first capture.
    CHECK(!fs::exists(fs::path(native.path) / L"Smaller.json") && read_profile(settings)["slots"][3] == first.entry.id);
    request.slot.reset(); request.expected_slot_id.reset();
    CHECK(save(fewer).entry.document == fewer); // Save As accepts the smaller selection.
    for (const auto name : {"AutoSave-foreign", "CON", "lpt9", "  "}) {
        request.name = name;
        reject([&]{save(fresh);});
    }

    request = {}; request.name = "Full"; request.scope = ProfileScope::full_tune;
    const ProfileJson full = {{"schema",2},{"device",{{"uuid",gpu},{"slot",pci}}},
        {"power_limit_mw",200000},{"current_limits_ma",{{"1",60000},{"2",55000}}},
        {"vf_deltas",{{"1",15000},{"2",30000}}}};
    const auto first_full = save(full);
    CHECK(first_full.entry.description.scope == ProfileScope::python_full_tune);
    CHECK(load_quick_profiles(settings, {}).save_scope == ProfileScope::full_tune);
    request.replace_id = first_full.entry.id; request.replace_document = full;
    auto partial = full;
    partial["current_limits_ma"].erase("2");
    reject([&]{save(partial);});  // Both captures still have power:*.
    partial = full; partial["vf_deltas"].erase("2");
    reject([&]{save(partial);});  // Both captures still have clock:core.
    request.scope = ProfileScope::daily;
    reject([&]{save(fresh);});
    CHECK(read_profile(first_full.entry.path) == full);
    request.scope = ProfileScope::full_tune;
    partial = full; partial["power_limit_mw"] = 220000;
    CHECK(save(partial).entry.document == partial);

    request = {}; request.name = "Durable"; request.slot = 0; request.expected_slot_id = "";
    // A readable settings file can still be held against replacement by another program.
    HANDLE held = CreateFileW(settings.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
    CHECK(held != INVALID_HANDLE_VALUE);
    const auto durable = save(fresh);
    CHECK(!durable.settings_error.empty() && !durable.slots && read_profile(durable.entry.path) == fresh);
    CloseHandle(held);
    CHECK(read_profile(settings)["slots"][0] == "");
    const auto after_failure = load_quick_profiles(settings, {});
    CHECK(after_failure.save_scope == ProfileScope::full_tune && !after_failure.save_slot);
}
void quick_profile_assignments() {
    Temp temp;
    const auto settings=(temp.path/L"Druta"/L"quick-profiles.json").wstring();
    const ProfileJson doc={{"format","druta-native-daily"},{"version",1},
        {"target",{{"uuid","GPU-test"},{"slot","0000:01:00.0"}}},
        {"controls",{{"core",{{"value",30},{"unit","MHz"}}}}}};
    std::vector<ProfileLibraryEntry> entries;
    for (const auto* id : {"undo","invalid","A","B","C","D","E","F","G"}) {
        ProfileLibraryEntry entry;
        entry.id=id; entry.document=doc; entry.description=inspect_profile(doc);
        entries.push_back(std::move(entry));
    }
    entries[0].autosave=true;
    entries[1].description=inspect_profile(ProfileJson::object());
    const auto defaults=load_quick_profiles(settings,entries);
    CHECK((defaults.ids==std::array<std::string,5>{"A","B","C","D","E"}));
    CHECK(!defaults.save_scope && !defaults.save_slot);
    // Two windows select individual fields. Each action merges with the current on-disk choices.
    auto legacy = read_profile(settings);
    legacy["future_option"] = {{"enabled", true}};
    write_profile(settings, legacy);
    const auto scoped = remember_toolbar_save_preferences(settings, {ProfileScope::curve});
    CHECK(scoped.save_scope == ProfileScope::curve && !scoped.save_slot);
    const auto numbered = remember_toolbar_save_preferences(settings, {std::nullopt, true, 4});
    CHECK(numbered.save_scope == ProfileScope::curve && numbered.save_slot == 4 && numbered.ids == defaults.ids);
    const std::vector<ProfileLibraryEntry> reordered(entries.rbegin(),entries.rend());
    CHECK(load_quick_profiles(settings,reordered).ids==defaults.ids);
    const auto swapped=assign_quick_profile(settings,entries,1,"E");
    CHECK((swapped.ids==std::array<std::string,5>{"A","E","C","D","B"}));
    const auto next_window = load_quick_profiles(settings, {});
    CHECK(next_window.save_scope == ProfileScope::curve && next_window.save_slot == 4);
    const auto library = remember_toolbar_save_preferences(settings, {std::nullopt, true, std::nullopt});
    CHECK(library.save_scope == ProfileScope::curve && !library.save_slot && library.ids == swapped.ids);
    const auto saved = read_profile(settings);
    CHECK(saved.at("future_option") == legacy.at("future_option") && saved.at("save_slot").is_null());
    reject([&]{remember_toolbar_save_preferences(settings, {ProfileScope::fan});});
    reject([&]{remember_toolbar_save_preferences(settings, {std::nullopt, true, 5});});
    CHECK(saved == read_profile(settings));
    auto newer = saved;
    newer["save_scope"] = "unknown";
    newer["save_slot"] = 999;
    write_profile(settings, newer);
    const auto fallback = load_quick_profiles(settings, {});
    CHECK(!fallback.save_scope && !fallback.save_slot && fallback.ids == swapped.ids);
    const auto replaced=assign_quick_profile(settings,entries,2,"F");
    CHECK((replaced.ids==std::array<std::string,5>{"A","E","F","D","B"}));
    entries.erase(entries.begin()+6); // E's portable file is offline
    std::reverse(entries.begin(),entries.end());
    CHECK(load_quick_profiles(settings,entries).ids==replaced.ids);
    reject([&]{assign_quick_profile(settings,entries,5,"A");});
    for (const auto* id : {"invalid","undo","missing","E"})
        reject([&]{assign_quick_profile(settings,entries,1,id);});
    CHECK(load_quick_profiles(settings,entries).ids==replaced.ids);
    const auto reassigned=assign_quick_profile(settings,entries,1,"G");
    CHECK(reassigned.ids[1]=="G"&&reassigned.ids[2]=="F");
    write_profile(settings,{{"version",1},{"slots",{"A","A","B","C","D"}}});
    reject([&]{load_quick_profiles(settings,entries);});
    reject([&]{assign_quick_profile(settings,entries,0,"F");});
    CHECK(read_profile(settings)["slots"][1]=="A");
}
}
int main(){discovery_and_listing();saving_and_history();quick_profile_assignments();toolbar_saving();std::printf("Profile library: %u checks, %u failures\n",checks,failures);return failures?1:0;}
