// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "profile_library.h"
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
struct Temp {fs::path path;Temp(){wchar_t base[MAX_PATH]{};if(!GetTempPathW(MAX_PATH,base))throw std::runtime_error("Temp root unavailable");path=fs::path(base)/(L"druta-profile-library-test-"+std::to_wstring(GetCurrentProcessId())+L"-"+std::to_wstring(GetTickCount64()));if(!fs::create_directory(path))throw std::runtime_error("Cannot create exclusive test directory");}~Temp(){std::error_code ec;fs::remove_all(path,ec);}};
void raw(const fs::path& path,const std::string& content){fs::create_directories(path.parent_path());std::ofstream out(path,std::ios::binary);out<<content;if(!out)throw std::runtime_error("Test fixture write failed");}
void stamp(const fs::path& path,uint64_t ticks){HANDLE file=CreateFileW(path.c_str(),FILE_WRITE_ATTRIBUTES,0,nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr);CHECK(file!=INVALID_HANDLE_VALUE);FILETIME time{static_cast<DWORD>(ticks),static_cast<DWORD>(ticks>>32)};CHECK(SetFileTime(file,nullptr,nullptr,&time)!=0);CloseHandle(file);}
void discovery_and_listing(){Temp temp;const auto source=temp.path/L"source",binary=source/L"native"/L"build",local=temp.path/L"local";raw(source/L"pyproject.toml","");fs::create_directories(source/L"src"/L"druta");fs::create_directories(source/L"profiles");fs::create_directories(binary/L"profiles");fs::create_directories(local/L"Thermetery"/L"Druta"/L"profiles");const auto native=local/L"Druta"/L"profiles";
 const auto dirs=discover_profile_directories({(binary/L"Druta.exe").wstring(),local.wstring()});CHECK(dirs.size()==4&&dirs[0].writable&&dirs[0].path==native.wstring());CHECK(!fs::exists(native));CHECK(list_profile_library(dirs).empty()&&!fs::exists(native));
 raw(source/L"profiles"/L"same.json",R"({"schema":2,"scope":"tuning_action","saved_ts":4000000000.25,"saved_at":"newer"})");raw(binary/L"profiles"/L"same.json",R"({"saved_ts":4000000000.125,"saved_at":"older"})");raw(local/L"Thermetery"/L"Druta"/L"profiles"/L"autosave-python.json",R"({"saved_ts":4000000000.0})");
 raw(source/L"profiles"/L"broken.json","{not json");raw(source/L"profiles"/L"oversize.json",std::string(profile_max_bytes+1,' '));raw(source/L"profiles"/L"array.json","[]");raw(source/L"profiles"/L"ignored.txt","{}");fs::create_directories(source/L"profiles"/L"directory.json");
 auto entries=list_profile_library(dirs);CHECK(entries.size()==6&&entries[0].source=="Python source"&&entries[0].saved_at=="newer"&&entries[1].source=="Portable"&&entries[2].autosave&&!entries[2].library_owned);CHECK(entries[0].id!=entries[1].id&&entries[0].name==entries[1].name);unsigned errors=0;for(const auto& entry:entries)if(!entry.error.empty()){++errors;CHECK(!entry.document);}CHECK(errors==3);CHECK(entries[0].document&&entries[0].document->at("schema")==2);
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
 raw(fs::path(native.path)/L"autosave-python-old.json","{}");raw(fs::path(external.path)/L"autosave-native-foreign-0000000000000000-00000000-00000000.json","{}");
 std::set<std::wstring> paths;std::wstring last;for(unsigned i=0;i<24;++i){last=save_library_autosave(native,"boost",document);CHECK(paths.insert(last).second);}CHECK(paths.size()==24&&fs::exists(last));auto entries=list_profile_library({native,external});unsigned own=0;for(const auto& e:entries)if(e.library_owned){++own;CHECK(e.autosave&&!e.saved_at.empty()&&e.document&&*e.document==document&&!e.document->contains("saved_ts"));}CHECK(own==20&&fs::exists(named)&&fs::exists(fs::path(native.path)/L"autosave-python-old.json")&&fs::exists(fs::path(external.path)/L"autosave-native-foreign-0000000000000000-00000000-00000000.json"));
 const auto before=list_profile_library({native}).size();reject([&]{save_library_autosave(native,"bad",ProfileJson::array());});CHECK(list_profile_library({native}).size()==before);
 // A file occupying the chosen destination directory fails without touching its bytes.
 const auto blocked=temp.path/L"blocked";raw(blocked,"keep");ProfileLibraryDirectory bad{blocked.wstring(),"Native",true};reject([&]{save_library_profile(bad,"name",document);});reject([&]{save_library_autosave(bad,"action",document);});std::ifstream check(blocked);std::string value;check>>value;CHECK(value=="keep");
 auto missing=list_profile_library({{(temp.path/L"missing").wstring(),"Missing",false}});CHECK(missing.empty()&&!fs::exists(temp.path/L"missing"));auto unavailable=list_profile_library({{blocked.wstring(),"Blocked",false}});CHECK(unavailable.size()==1&&!unavailable[0].error.empty()&&!unavailable[0].document);
}
}
int main(){discovery_and_listing();saving_and_history();std::printf("Profile library: %u checks, %u failures\n",checks,failures);return failures?1:0;}
