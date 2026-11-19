// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_weight_receipt.h"
#include "profile_io.h"
#include "../src/core/sha256.h"
#include <windows.h>
#include <shlobj.h>
#include <algorithm>
#include <atomic>
#include <cstring>
#include <cstdio>
#include <filesystem>
#include <regex>
#include <set>
#include <stdexcept>
#include <utility>
namespace druta::ui {
namespace {
using Json=ProfileJson;
void require(bool b,const char* s){if(!b)throw std::runtime_error(s);}
void win(bool b,const char* s){if(!b)throw std::runtime_error(std::string(s)+" (Windows error "+std::to_string(GetLastError())+")");}
std::string string(const Json& j,size_t cap,bool empty=false){require(j.is_string(),"Weight receipt string required");auto s=j.get<std::string>();require((empty||!s.empty())&&s.size()<=cap&&s.find('\0')==std::string::npos,"Weight receipt string invalid");return s;}
uint32_t number(const Json& j,uint32_t cap=UINT32_MAX){require(j.is_number_integer(),"Weight receipt integer required");if(j.is_number_unsigned()){auto n=j.get<uint64_t>();require(n<=cap,"Weight receipt integer out of range");return static_cast<uint32_t>(n);}auto n=j.get<int64_t>();require(n>=0&&static_cast<uint64_t>(n)<=cap,"Weight receipt integer out of range");return static_cast<uint32_t>(n);}
void keys(const Json& j,std::initializer_list<const char*> fields){require(j.is_object()&&j.size()==fields.size(),"Weight receipt schema invalid");for(auto f:fields)require(j.contains(f),"Weight receipt field missing");}
std::string hex(const std::vector<uint8_t>& b){static const char* digits="0123456789abcdef";std::string s;s.reserve(b.size()*2);for(auto c:b){s+=digits[c>>4];s+=digits[c&15];}return s;}
std::vector<uint8_t> unhex(const Json& j,size_t cap){auto s=string(j,cap*2,true);require(s.size()%2==0,"Weight receipt hex length invalid");auto nib=[](char c)->uint8_t{if(c>='0'&&c<='9')return static_cast<uint8_t>(c-'0');if(c>='a'&&c<='f')return static_cast<uint8_t>(c-'a'+10);throw std::runtime_error("Weight receipt canonical hex required");};std::vector<uint8_t> b(s.size()/2);for(size_t i=0;i<b.size();++i)b[i]=static_cast<uint8_t>((nib(s[2*i])<<4)|nib(s[2*i+1]));return b;}
std::string utf8(const std::wstring& s){require(s.size()<=32768&&s.find(L'\0')==std::wstring::npos,"Weight registry name invalid");if(s.empty())return {};int n=WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,s.data(),static_cast<int>(s.size()),nullptr,0,nullptr,nullptr);require(n>0,"Weight registry name Unicode invalid");std::string out(static_cast<size_t>(n),0);win(WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,s.data(),static_cast<int>(s.size()),out.data(),n,nullptr,nullptr)==n,"Encode weight name");return out;}
std::wstring wide(const std::string& s){if(s.empty())return {};int n=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,s.data(),static_cast<int>(s.size()),nullptr,0);require(n>0,"Weight registry name UTF-8 invalid");std::wstring out(static_cast<size_t>(n),0);win(MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,s.data(),static_cast<int>(s.size()),out.data(),n)==n,"Decode weight name");return out;}
std::string fold(std::string s){for(auto& c:s)if(c>='A'&&c<='Z')c=static_cast<char>(c+'a'-'A');return s;}
bool managed(const std::string& s){static const std::regex re("^(romoverride[0-9a-f]{2}|vbios|enablegpufirmware)$");return std::regex_match(fold(s),re);}
void identity(const PowerWeightReceipt& r){static const std::regex id("^[0-9a-f]{32}$"),slot("^0000:[0-9a-f]{2}:[01][0-9a-f]\\.[0-7]$"),key(R"(^SYSTEM\\CurrentControlSet\\Control\\Class\\\{4d36e968-e325-11ce-bfc1-08002be10318\}\\[0-9]{4}$)",std::regex::icase);
 require(std::regex_match(r.id,id)&&std::regex_match(r.identity.slot,slot),"Weight receipt id/slot invalid");string(Json(r.uuid),160);require(r.windows.slot==r.identity.slot&&std::regex_match(r.windows.driver_key,key),"Weight receipt Windows target invalid");
 char prefix[64]{};std::snprintf(prefix,sizeof(prefix),"PCI\\VEN_10DE&DEV_%04X&SUBSYS_%08X",r.identity.device_id,r.identity.subsystem_id);const auto instance=fold(r.windows.instance_id),p=fold(prefix);require(instance.size()<=32768&&instance.find('\0')==std::string::npos&&instance.compare(0,p.size(),p)==0&&instance.size()>p.size()&&(instance[p.size()]=='&'||instance[p.size()]=='\\')&&instance.find('\\',p.size())!=std::string::npos,"Weight receipt Windows device differs from logical target");string(Json(r.phase),64);string(Json(r.message),8192,true);
}
Json parse(const std::string& bytes){require(!bytes.empty()&&bytes.size()<=power_weight_receipt_max_bytes,"Weight receipt size exceeds bound");std::vector<std::set<std::string>> stack;auto callback=[&](int depth,Json::parse_event_t event,Json& value){require(depth<=24,"Weight receipt JSON too deep");if(event==Json::parse_event_t::object_start)stack.emplace_back();else if(event==Json::parse_event_t::object_end)stack.pop_back();else if(event==Json::parse_event_t::key)require(stack.back().insert(value.get<std::string>()).second,"Weight receipt duplicate JSON key");return true;};return Json::parse(bytes,callback);}
std::string new_id(){static std::atomic<uint32_t> serial{0};FILETIME t{};GetSystemTimeAsFileTime(&t);char out[33]{};std::snprintf(out,sizeof(out),"%08x%08x%08x%08x",static_cast<unsigned>(t.dwHighDateTime),static_cast<unsigned>(t.dwLowDateTime),static_cast<unsigned>(GetCurrentProcessId()),static_cast<unsigned>(serial.fetch_add(1)));return out;}
template<size_t N>std::string bounded(const char(&s)[N]){const auto* e=static_cast<const char*>(std::memchr(s,0,N));require(e,"Weight adapter string unterminated");return {s,e};}
std::wstring directory(const std::wstring& input,bool create){const auto requested=input.empty()?power_weight_receipt_directory():input;require(!requested.empty()&&requested.find(L'\0')==std::wstring::npos,"Weight store directory invalid");const auto path=std::filesystem::absolute(requested).lexically_normal();if(create)std::filesystem::create_directories(path);const auto a=GetFileAttributesW(path.c_str());if(a!=INVALID_FILE_ATTRIBUTES)require((a&FILE_ATTRIBUTE_DIRECTORY)&&!(a&FILE_ATTRIBUTE_REPARSE_POINT),"Weight store must be a real directory");return path.wstring();}
std::wstring child(const std::wstring& dir,const std::wstring& file){return (std::filesystem::path(dir)/file).wstring();}
std::wstring active_path(const std::wstring& dir,const std::string& slot,const std::string& uuid){string(Json(slot),23);string(Json(uuid),160);const auto value=slot+"\n"+uuid;const auto digest=druta::sha256_hex(reinterpret_cast<const uint8_t*>(value.data()),value.size());return child(dir,L"active-"+std::wstring(digest.begin(),digest.end())+L".json");}
struct File {HANDLE h;explicit File(HANDLE x):h(x){}~File(){if(h!=INVALID_HANDLE_VALUE)CloseHandle(h);}File(const File&)=delete;File&operator=(const File&)=delete;};
struct StoreLock {File file;explicit StoreLock(const std::wstring& dir):file(CreateFileW(child(dir,L"store.lock").c_str(),GENERIC_READ|GENERIC_WRITE,0,nullptr,OPEN_ALWAYS,FILE_ATTRIBUTE_NORMAL,nullptr)){win(file.h!=INVALID_HANDLE_VALUE,"Weight receipt store busy or unavailable");}};
std::optional<std::string> read_bytes(const std::wstring& path){File f(CreateFileW(path.c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL|FILE_FLAG_OPEN_REPARSE_POINT,nullptr));if(f.h==INVALID_HANDLE_VALUE){const auto e=GetLastError();if(e==ERROR_FILE_NOT_FOUND||e==ERROR_PATH_NOT_FOUND)return {};win(false,"Open weight receipt");}BY_HANDLE_FILE_INFORMATION meta{};win(GetFileInformationByHandle(f.h,&meta)!=0,"Inspect weight receipt");require(!(meta.dwFileAttributes&(FILE_ATTRIBUTE_DIRECTORY|FILE_ATTRIBUTE_REPARSE_POINT)),"Weight receipt must be a regular direct-child file");LARGE_INTEGER size{};win(GetFileSizeEx(f.h,&size)!=0,"Size weight receipt");require(size.QuadPart>0&&size.QuadPart<=static_cast<LONGLONG>(power_weight_receipt_max_bytes),"Weight receipt file size invalid");std::string bytes(static_cast<size_t>(size.QuadPart),0);DWORD got=0;win(ReadFile(f.h,bytes.data(),static_cast<DWORD>(bytes.size()),&got,nullptr)!=0,"Read weight receipt");require(got==bytes.size(),"Weight receipt truncated");char c=0;win(ReadFile(f.h,&c,1,&got,nullptr)!=0,"Finish weight receipt read");require(!got,"Weight receipt grew during read");return bytes;}
void atomic_write(const std::wstring& path,const std::string& bytes,bool replace){require(bytes.size()<=power_weight_receipt_max_bytes,"Weight receipt exceeds storage bound");const auto tmp=path+L".tmp-"+wide(new_id());{File file(CreateFileW(tmp.c_str(),GENERIC_WRITE,0,nullptr,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,nullptr));win(file.h!=INVALID_HANDLE_VALUE,"Create weight journal temporary");try{DWORD wrote=0;win(WriteFile(file.h,bytes.data(),static_cast<DWORD>(bytes.size()),&wrote,nullptr)!=0&&wrote==bytes.size(),"Write weight journal");win(FlushFileBuffers(file.h)!=0,"Flush weight journal");}catch(...){CloseHandle(file.h);file.h=INVALID_HANDLE_VALUE;DeleteFileW(tmp.c_str());throw;}}
 if(!MoveFileExW(tmp.c_str(),path.c_str(),MOVEFILE_WRITE_THROUGH|(replace?MOVEFILE_REPLACE_EXISTING:0))){const auto e=GetLastError();DeleteFileW(tmp.c_str());SetLastError(e);win(false,"Commit weight journal");}}
Json pointer(const PowerWeightReceipt& r){return Json{{"format","druta.power-weight-active"},{"version",1},{"id",r.id},{"slot",r.identity.slot},{"uuid",r.uuid}};}
std::optional<std::string> pointer_id(const std::wstring& path,const std::string& slot,const std::string& uuid,
                                      std::string* predecessor=nullptr) {
 auto bytes=read_bytes(path);if(!bytes)return {};
 auto j=parse(*bytes);
 static const std::regex id_pattern("^[0-9a-f]{32}$");
 const auto version=number(j.at("version"));
 if(version==2) {
  keys(j,{"format","version","id","slot","uuid","predecessor"});
  const auto prior=string(j.at("predecessor"),32);
  require(std::regex_match(prior,id_pattern),"Invalid prior clock receipt id");
  if(predecessor)*predecessor=prior;
 } else {
  keys(j,{"format","version","id","slot","uuid"});
  require(version==1,"Unsupported weight pointer version");
 }
 require(string(j.at("format"),64)=="druta.power-weight-active"&&string(j.at("slot"),23)==slot&&
         string(j.at("uuid"),160)==uuid,"Weight active pointer identity invalid");
 const auto id=string(j.at("id"),32);
 require(std::regex_match(id,id_pattern),"Weight active receipt id invalid");
 return id;
}
}
namespace {
std::vector<PowerWeightRegistryPatch> clock_patches(const std::vector<uint8_t>& before,
                                                    const std::vector<uint8_t>& after) {
 require(before.size()==after.size(),"Clock planner changed ROM length");
 std::vector<PowerWeightRegistryPatch> patches;
 for(size_t i=0;i<before.size();) {
  if(before[i]==after[i]) { ++i; continue; }
  const size_t start=i;
  while(i<before.size()&&before[i]!=after[i]) ++i;
  patches.push_back({static_cast<uint32_t>(start),{before.begin()+start,before.begin()+i},
                     {after.begin()+start,after.begin()+i}});
 }
 return patches;
}

PowerWeightReceipt captured_receipt(const PowerWeightCapture& c) {
 require(c.image_availability==VoltagePolicyAvailability::available&&
         c.registry_availability==VoltagePolicyAvailability::available&&c.windows&&c.registry,
         "Current ROM and registry capture required");
 require(c.adapter.size==sizeof(c.adapter)&&c.target.size==sizeof(c.target)&&c.adapter.nvapi_ok&&
         c.adapter.nvapi_selected&&c.target.handle&&c.target.handle==c.adapter.nvapi_handle&&
         bounded(c.adapter.slot)==c.identity.slot&&bounded(c.target.slot)==c.identity.slot&&
         bounded(c.adapter.pairing_error).empty()&&c.adapter.nvapi_devid==c.identity.device_id&&
         c.adapter.nvapi_subsys==c.identity.subsystem_id&&c.target.device_id==c.identity.device_id&&
         c.target.subsystem_id==c.identity.subsystem_id&&c.target.architecture==c.identity.architecture,
         "Captured ROM target is not paired");
 PowerWeightReceipt r;
 r.id=new_id();r.uuid=bounded(c.adapter.uuid);r.identity=c.identity;r.windows=*c.windows;
 r.image=c.image;r.before=*c.registry;
 return r;
}
const char* domain_name(PascalClockDomain domain) {
 switch(domain) {
  case PascalClockDomain::gpc:return "gpc";
  case PascalClockDomain::xbar:return "xbar";
  case PascalClockDomain::sys:return "sys";
  case PascalClockDomain::tfaw:return "tfaw";
 }
 throw std::runtime_error("Unknown Pascal clock domain");
}
PascalClockDomain domain_value(const Json& value) {
 const auto name=string(value,8);
 if(name=="gpc")return PascalClockDomain::gpc;
 if(name=="xbar")return PascalClockDomain::xbar;
 if(name=="sys")return PascalClockDomain::sys;
 if(name=="tfaw")return PascalClockDomain::tfaw;
 throw std::runtime_error("Unknown Pascal clock domain");
}

bool same_clock_field(const PascalClockEdit& a,const PascalClockEdit& b) {
 return a.domain==b.domain&&a.pstate==b.pstate;
}
bool same_clock_edits(const std::vector<PascalClockEdit>& a,const std::vector<PascalClockEdit>& b) {
 return std::equal(a.begin(),a.end(),b.begin(),b.end(),[](const auto& lhs,const auto& rhs) {
  return same_clock_field(lhs,rhs)&&lhs.expected==rhs.expected&&lhs.value==rhs.value&&lhs.timing_sample==rhs.timing_sample;
 });
}
bool same_registry(const PowerWeightRegistrySnapshot& a,const PowerWeightRegistrySnapshot& b) {
 if(a.size()!=b.size())return false;
 return std::all_of(a.begin(),a.end(),[&](const auto& lhs) {
  const auto name=fold(utf8(lhs.name));
  const auto found=std::find_if(b.begin(),b.end(),[&](const auto& rhs){return fold(utf8(rhs.name))==name;});
  return found!=b.end()&&lhs.type==found->type&&lhs.bytes==found->bytes;
 });
}
bool same_clock_target(const PowerWeightReceipt& a,const PowerWeightReceipt& b) {
 return a.uuid==b.uuid&&a.identity.slot==b.identity.slot&&a.identity.architecture==b.identity.architecture&&
        a.identity.device_id==b.identity.device_id&&a.identity.subsystem_id==b.identity.subsystem_id&&
        a.windows.slot==b.windows.slot&&a.windows.instance_id==b.windows.instance_id&&
        a.windows.driver_key==b.windows.driver_key;
}
bool revisable(const PowerWeightReceipt& active) {
 return active.is_pascal_clocks()&&active.phase=="active"&&!active.uncertain&&!active.restoring;
}
Json encode_clock_edits(const std::vector<PascalClockEdit>& edits) {
 Json out=Json::array();
 for(const auto& q:edits) {
  Json entry={{"domain",domain_name(q.domain)},{"pstate",q.pstate},{"before_raw",q.expected},{"after_raw",q.value}};
  if(q.timing_sample) {
   const auto& sample=*q.timing_sample;
   entry["p0"]={{"memory_mhz",sample.memory_mhz},{"config1",sample.config1},{"config3",sample.config3}};
  }
  out.push_back(std::move(entry));
 }
 return out;
}
std::vector<PascalClockEdit> decode_clock_edits(const Json& rows) {
 require(rows.is_array()&&!rows.empty()&&rows.size()<=50,"Clock receipt request inventory invalid");
 std::vector<PascalClockEdit> out;
 for(const auto& q:rows) {
  const auto domain=domain_value(q.at("domain"));
  std::optional<PascalTimingSample> timing;
  if(domain==PascalClockDomain::tfaw) {
   keys(q,{"domain","pstate","after_raw","before_raw","p0"});
   const auto& sample=q.at("p0");keys(sample,{"memory_mhz","config1","config3"});
   timing=PascalTimingSample{int(number(sample.at("memory_mhz"),16383)),number(sample.at("config1")),number(sample.at("config3"))};
  } else keys(q,{"domain","pstate","after_raw","before_raw"});
  const int pstate=q["pstate"].is_number_integer()&&q["pstate"]==-1?-1:static_cast<int>(number(q["pstate"],15));
  out.push_back({domain,pstate,static_cast<int>(number(q["before_raw"],16383)),
                 static_cast<int>(number(q["after_raw"],16383)),timing});
 }
 return out;
}
}

std::optional<PascalTimingSample> receipt_timing_sample(const PowerWeightReceipt& r, PascalTimingPhase phase) {
 const auto timing=[](const auto& edits) -> const PascalClockEdit* {
  auto it=std::find_if(edits.begin(),edits.end(),[](const auto& e){return e.domain==PascalClockDomain::tfaw;});
  return it==edits.end()?nullptr:&*it;
 };
 const auto* current=timing(r.clocks);
 const auto* previous=timing(r.previous_clocks);
 if(!current&&!previous)return {};
 const auto& baseline=(current?current:previous)->timing_sample;
 require(baseline.has_value()&&(!current||!previous||current->timing_sample==previous->timing_sample),
         "P0 controller fingerprint changed between receipt revisions");
 if(phase==PascalTimingPhase::previous&&previous)return with_tfaw(*baseline,previous->value);
 if(phase==PascalTimingPhase::applied&&current)return with_tfaw(*baseline,current->value);
 return baseline;
}

PowerWeightReceiptDerived derive_weight_receipt(const PowerWeightReceipt& r) {
 identity(r);
 require(r.image.size()>=32&&r.image.size()<=0xfa000,"Receipt ROM length invalid");
 require(r.before.size()<=258,"Registry inventory exceeds bound");
 std::set<std::string> names;
 for(const auto& v:r.before) {
  const auto name=utf8(v.name);
  require(managed(name)&&v.bytes.size()<=0x100000&&names.insert(fold(name)).second,"Registry baseline malformed");
 }
 PowerWeightReceiptDerived out;
 uint32_t version=0;uint8_t oem=0;
 std::vector<PowerWeightRegistryPatch> patches;
 if(r.is_pascal_clocks()) {
  require(clock_override_architecture(r.identity.architecture)&&r.requests.empty()&&r.info.empty()&&r.clocks.size()<=50,
          "Clock receipt geometry or architecture invalid");
  const auto table=inspect_pascal_clocks(r.image,r.identity.architecture);
  require(table.ok,table.message.c_str());
  require(table.device_id==r.identity.device_id,"Clock ROM PCI device differs from selected GPU");
  out.clock_plan=plan_pascal_clocks(r.image,r.clocks,r.identity.architecture);
  require(out.clock_plan.ok,out.clock_plan.message.c_str());
  if(!r.previous_clocks.empty()) {
   require(r.previous_clocks.size()<=50,"Previous Pascal clock receipt geometry invalid");
   auto previous=plan_pascal_clocks(r.image,r.previous_clocks,r.identity.architecture);
   require(previous.ok,previous.message.c_str());
   require(previous.image!=r.image&&previous.image!=out.clock_plan.image,"Clock revision requires a distinct running image");
   out.clock_source_image=std::move(previous.image);
  }
  receipt_timing_sample(r,PascalTimingPhase::original);
  version=table.selector_version;oem=table.selector_oem;
  patches=clock_patches(r.image,out.clock_plan.image);
 } else {
  require(r.previous_clocks.empty()&&r.info.size()==20512&&!r.requests.empty()&&r.requests.size()<=32,
          "Weight receipt evidence geometry invalid");
  out.plan=plan_power_relationship_weights(r.image,r.identity,r.layout,r.info,r.requests);
  require(out.plan.ok,out.plan.message.c_str());
  for(const auto& request:r.requests)require(request.expected_raw.has_value(),"Weight receipt request requires original raw baseline");
  if(!out.plan.patches.empty()) {
   const auto parsed=inspect_power_weight_image(r.image);
   version=parsed.selector_version;oem=parsed.selector_oem;
   for(const auto& p:out.plan.patches)patches.push_back({p.offset,p.before,p.after});
  }
 }
 if(patches.empty())out.after=r.before;
 else out.after=project_power_weight_registry(r.before,r.image,version,oem,patches).after;
 std::set<std::string> issued;
 for(const auto& n:r.issued_names) {
  const auto key=fold(utf8(n));
  require(issued.insert(key).second&&!names.count(key)&&std::any_of(out.after.begin(),out.after.end(),
          [&](const auto& v){return fold(utf8(v.name))==key;}),"Issued registry name is not a derived owned addition");
 }
 return out;
}
PowerWeightReceipt make_weight_receipt(const PowerWeightCapture& c,const std::vector<PowerWeightRequest>& requests) {
 require(c.info_availability==VoltagePolicyAvailability::available,"Complete weight INFO required");
 auto r=captured_receipt(c);r.layout=c.layout;r.info=c.info;r.requests=requests;
 const auto plan=plan_power_relationship_weights(r.image,r.identity,r.layout,r.info,r.requests);
 require(plan.ok,plan.message.c_str());
 for(auto& request:r.requests) {
  const auto found=std::find_if(plan.selected.begin(),plan.selected.end(),[&](const auto& s){return s.index==request.index;});
  require(found!=plan.selected.end(),"Weight request missing from plan");request.expected_raw=found->before_raw;
 }
 derive_weight_receipt(r);return r;
}
PowerWeightReceipt make_pascal_clock_receipt(const PowerWeightCapture& c,const std::vector<PascalClockEdit>& edits) {
 require(!edits.empty(),"Select at least one Pascal clock change");
 auto r=captured_receipt(c);r.clocks=edits;
 if(auto needed=receipt_timing_sample(r,PascalTimingPhase::original))
  require(c.timing_sample==needed,"P0 controller timing or memory clock changed since review");
 derive_weight_receipt(r);return r;
}
PowerWeightReceipt revise_pascal_clock_receipt(const PowerWeightCapture& c,const PowerWeightReceipt& active,
                                               const std::vector<PascalClockEdit>& edits) {
 require(revisable(active),"Verify or restore the previous clock override before editing");
 const auto previous=derive_weight_receipt(active);
 if(auto inherited=receipt_timing_sample(active,PascalTimingPhase::applied))
  require(c.timing_sample==inherited,"The active timing override must be verified at the same P0 memory clock before a revision");
 auto r=captured_receipt(c);
 require(same_clock_target(active,r),"Clock revision target differs from the active override");
 require(c.image==previous.clock_plan.image&&c.image!=active.image,
         "The running image differs from the verified clock override");
 require(same_registry(active.before,r.before),"The clock override's original registry settings changed");
 const auto original=inspect_pascal_clocks(active.image,active.identity.architecture);
 for(const auto& edit:edits)
  if(edit.domain==PascalClockDomain::tfaw)
   require(edit.timing_sample && c.timing_sample==edit.timing_sample,"P0 controller timing or memory clock changed since review");
 const auto wanted=plan_pascal_clocks(c.image,edits,active.identity.architecture,&original);
 require(wanted.ok,wanted.message.c_str());
 require(wanted.image!=c.image,"The selected clocks already match the running image");

 // The restart loads from the original image. Stage all effective edits on that image.
 r.image=active.image;
 r.before=active.before;
 r.previous_clocks=active.clocks;
 r.clocks=active.clocks;
 for(const auto& edit:edits) {
  const auto existing=std::find_if(r.clocks.begin(),r.clocks.end(),[&](const auto& row){return same_clock_field(row,edit);});
  auto rebased=edit;
  if(edit.domain==PascalClockDomain::tfaw) {
   if(existing!=r.clocks.end()) {
    require(with_tfaw(*existing->timing_sample,existing->value)==*edit.timing_sample,
            "P0 memory band or controller fingerprint differs from the active timing override");
    rebased.expected=existing->expected;
    rebased.timing_sample=existing->timing_sample;
   }
  } else {
   const auto field=std::find_if(original.fields.begin(),original.fields.end(),[&](const auto& row) {
    return row.domain==edit.domain&&row.pstate==edit.pstate;
   });
   require(field!=original.fields.end(),"Selected clock field is missing from the original image");
   rebased.expected=field->current;
  }
  if(existing==r.clocks.end())r.clocks.push_back(rebased);
  else *existing=rebased;
 }
 const auto first=r.clocks.front();
 r.clocks.erase(std::remove_if(r.clocks.begin(),r.clocks.end(),[](const auto& row){return row.expected==row.value;}),r.clocks.end());
 if(r.clocks.empty())r.clocks.push_back(first);
 require(derive_weight_receipt(r).clock_plan.image==wanted.image,"Clock revision does not reproduce the reviewed edits");
 return r;
}
std::string encode_weight_receipt(const PowerWeightReceipt& r) {
 derive_weight_receipt(r);
 Json requests=Json::array(),before=Json::array(),issued=Json::array();
 for(const auto& v:r.before)before.push_back({{"name",utf8(v.name)},{"type",v.type},{"bytes",hex(v.bytes)}});
 for(const auto& n:r.issued_names)issued.push_back(utf8(n));
 Json j={{"version",r.previous_clocks.empty()?1:2},{"id",r.id},{"uuid",r.uuid},
         {"identity",{{"slot",r.identity.slot},{"architecture",r.identity.architecture},{"device_id",r.identity.device_id},{"subsystem_id",r.identity.subsystem_id}}},
         {"windows",{{"slot",r.windows.slot},{"instance_id",r.windows.instance_id},{"driver_key",r.windows.driver_key}}},
         {"image",hex(r.image)},{"before",before},{"phase",r.phase},{"message",r.message},{"issued_names",issued},
         {"refresh_requested",r.refresh_requested},{"uncertain",r.uncertain},{"restoring",r.restoring}};
 if(r.is_pascal_clocks()) {
  requests=encode_clock_edits(r.clocks);
  if(!r.previous_clocks.empty())j["previous_requests"]=encode_clock_edits(r.previous_clocks);
  j["format"]="druta.pascal-clock-session";
 } else {
  for(const auto& q:r.requests)requests.push_back({{"index",q.index},{"after_raw",power_weight_raw(q.multiplier)},{"before_raw",*q.expected_raw}});
  j["format"]="druta.power-weight-receipt";
  j["layout"]={{"info_command",r.layout.info_command},{"info_size",r.layout.info_size},{"policy_base",r.layout.policy_base},{"policy_stride",r.layout.policy_stride}};
  j["info"]=hex(r.info);
 }
 j["requests"]=std::move(requests);
 auto bytes=j.dump();require(bytes.size()<=power_weight_receipt_max_bytes,"Receipt exceeds serialization bound");return bytes;
}
PowerWeightReceipt decode_weight_receipt(const std::string& bytes) {
 auto j=parse(bytes);
 const auto format=string(j.at("format"),64);
 const bool clock=format=="druta.pascal-clock-session";
 const auto version=number(j.at("version"));
 require((clock&&(version==1||version==2))||(format=="druta.power-weight-receipt"&&version==1),"Unsupported receipt format");
 if(clock&&version==2)keys(j,{"format","version","id","uuid","identity","windows","image","requests","previous_requests","before","phase","message","issued_names","refresh_requested","uncertain","restoring"});
 else if(clock)keys(j,{"format","version","id","uuid","identity","windows","image","requests","before","phase","message","issued_names","refresh_requested","uncertain","restoring"});
 else keys(j,{"format","version","id","uuid","identity","windows","layout","image","info","requests","before","phase","message","issued_names","refresh_requested","uncertain","restoring"});
 PowerWeightReceipt r;r.id=string(j["id"],32);r.uuid=string(j["uuid"],160);
 const auto& i=j["identity"];keys(i,{"slot","architecture","device_id","subsystem_id"});
 r.identity={string(i["slot"],23),number(i["architecture"]),number(i["device_id"],65535),number(i["subsystem_id"])};
 const auto& w=j["windows"];keys(w,{"slot","instance_id","driver_key"});
 r.windows={string(w["slot"],23),string(w["instance_id"],32768),string(w["driver_key"],512)};
 r.image=unhex(j["image"],0xfa000);
 require(j["requests"].is_array()&&!j["requests"].empty()&&j["requests"].size()<=(clock?50u:32u),"Receipt request inventory invalid");
 if(clock) {
  r.clocks=decode_clock_edits(j["requests"]);
  if(version==2)r.previous_clocks=decode_clock_edits(j["previous_requests"]);
 } else {
  const auto& l=j["layout"];keys(l,{"info_command","info_size","policy_base","policy_stride"});
  r.layout={number(l["info_command"]),number(l["info_size"]),number(l["policy_base"]),number(l["policy_stride"])};
  r.info=unhex(j["info"],20512);
  for(const auto& q:j["requests"]) {
   keys(q,{"index","after_raw","before_raw"});
   r.requests.push_back({number(q["index"],31),double(number(q["after_raw"],65535))/4096.0,static_cast<uint16_t>(number(q["before_raw"],65535))});
  }
 }
 require(j["before"].is_array()&&j["before"].size()<=258,"Receipt registry inventory invalid");
 for(const auto& v:j["before"]) {
  keys(v,{"name","type","bytes"});r.before.push_back({wide(string(v["name"],256)),number(v["type"]),unhex(v["bytes"],0x100000)});
 }
 r.phase=string(j["phase"],64);r.message=string(j["message"],8192,true);
 require(j["issued_names"].is_array()&&j["issued_names"].size()<=256,"Receipt issued inventory invalid");
 for(const auto& n:j["issued_names"])r.issued_names.push_back(wide(string(n,256)));
 require(j["refresh_requested"].is_boolean()&&j["uncertain"].is_boolean()&&j["restoring"].is_boolean(),"Receipt journal Boolean required");
 r.refresh_requested=j["refresh_requested"].get<bool>();r.uncertain=j["uncertain"].get<bool>();r.restoring=j["restoring"].get<bool>();
 derive_weight_receipt(r);return r;
}
std::wstring power_weight_receipt_directory() {
 const auto n=GetEnvironmentVariableW(L"LOCALAPPDATA",nullptr,0);
 std::wstring root;
 if(n) {
  require(n>1&&n<=32768,"Invalid LOCALAPPDATA for weight journal");
  root.resize(n);
  const auto got=GetEnvironmentVariableW(L"LOCALAPPDATA",root.data(),n);
  require(got&&got<n,"LOCALAPPDATA changed during resolution");
  root.resize(got);
 } else {
  // XP x64 profiles can have a Local AppData shell folder without this environment variable.
  wchar_t folder[MAX_PATH]{};
  require(SUCCEEDED(SHGetFolderPathW(nullptr,CSIDL_LOCAL_APPDATA,nullptr,SHGFP_TYPE_CURRENT,folder))&&folder[0],
          "Cannot locate this account's local clock journal");
  root=folder;
 }
 return (std::filesystem::path(root)/L"Druta"/L"power-weights").wstring();
}
std::wstring power_weight_receipt_path(const std::wstring& dir,const std::string& id){require(std::regex_match(id,std::regex("^[0-9a-f]{32}$")),"Weight receipt path id invalid");return child(directory(dir,false),L"receipt-"+wide(id)+L".json");}
std::wstring power_weight_operation_lock_path(const std::wstring& dir,const std::string& id) {
 require(std::regex_match(id,std::regex("^[0-9a-f]{32}$")),"Weight operation lock id invalid");
 // A protected boot enrollment can outlive the ordinary per-user receipt folder.
 return child(directory(dir,true),L"operation-"+wide(id)+L".lock");
}
PowerWeightReceipt read_weight_receipt(const std::wstring& dir,const std::string& id){auto bytes=read_bytes(power_weight_receipt_path(dir,id));require(bytes.has_value(),"Weight receipt not found");auto r=decode_weight_receipt(*bytes);require(r.id==id,"Weight receipt filename/id mismatch");return r;}
void create_weight_receipt(const std::wstring& dir,const PowerWeightReceipt& r){require(r.previous_clocks.empty(),"Clock revisions must replace their verified active receipt");const auto bytes=encode_weight_receipt(r);const auto root=directory(dir,true);StoreLock lock(root);const auto active=active_path(root,r.identity.slot,r.uuid);require(!pointer_id(active,r.identity.slot,r.uuid),"An active weight receipt already exists for this adapter");atomic_write(power_weight_receipt_path(root,r.id),bytes,false);atomic_write(active,pointer(r).dump(),false);}
void replace_pascal_clock_receipt(const std::wstring& dir,const PowerWeightReceipt& active,
                                  const PowerWeightReceipt& replacement) {
 require(revisable(active)&&replacement.is_pascal_clocks()&&replacement.phase=="prepared"&&
         replacement.issued_names.empty()&&!replacement.refresh_requested&&!replacement.uncertain&&
         !replacement.restoring&&active.id!=replacement.id,"Clock revision requires an unused prepared receipt");
 require(same_clock_target(active,replacement)&&active.image==replacement.image&&
         same_registry(active.before,replacement.before)&&same_clock_edits(active.clocks,replacement.previous_clocks),
         "Clock revision does not preserve the active target and original settings");
 const auto previous_bytes=encode_weight_receipt(active);
 const auto bytes=encode_weight_receipt(replacement);
 const auto root=directory(dir,false);

 // A helper may already be verifying or restoring the previous receipt. Use
 // its existing lock before comparing or replacing the active pointer.
 File operation(CreateFileW(power_weight_operation_lock_path(root,active.id).c_str(),GENERIC_READ|GENERIC_WRITE,
                            0,nullptr,OPEN_ALWAYS,FILE_ATTRIBUTE_NORMAL|FILE_FLAG_DELETE_ON_CLOSE,nullptr));
 win(operation.h!=INVALID_HANDLE_VALUE,"The active clock override is in use or its lock cannot be opened");
 StoreLock lock(root);
 const auto path=active_path(root,active.identity.slot,active.uuid);
 require(pointer_id(path,active.identity.slot,active.uuid)==std::optional<std::string>(active.id)&&
         encode_weight_receipt(read_weight_receipt(root,active.id))==previous_bytes,
         "The active clock override changed. Read it again before applying changes");
 atomic_write(power_weight_receipt_path(root,replacement.id),bytes,false);
 auto next=pointer(replacement);
 next["version"]=2;next["predecessor"]=active.id;
 atomic_write(path,next.dump(),true);
}
bool unused_clock_preparation(const PowerWeightReceipt& r) {
 // "writing" is published before the first write intent. Every dispatch first persists its name and uncertainty.
 return r.is_pascal_clocks() && (r.phase=="prepared" || r.phase=="writing") && r.issued_names.empty() &&
        !r.refresh_requested && !r.uncertain && !r.restoring;
}
void abandon_prepared_clock_receipt(const std::wstring& dir,const PowerWeightReceipt& prepared) {
 require(unused_clock_preparation(prepared),"Clock trial has already started");
 const auto root=directory(dir,false);
 StoreLock lock(root);
 const auto path=active_path(root,prepared.identity.slot,prepared.uuid);
 std::string predecessor;
 require(pointer_id(path,prepared.identity.slot,prepared.uuid,&predecessor)==prepared.id&&
         encode_weight_receipt(read_weight_receipt(root,prepared.id))==encode_weight_receipt(prepared),
         "Clock preview has changed since preparation");
 if(prepared.previous_clocks.empty()) {
  require(predecessor.empty(),"Unexpected prior clock receipt");
  win(DeleteFileW(path.c_str())!=0,"Retire unused clock preview");
  return;
 }
 require(!predecessor.empty(),"This older clock revision requires manual verification");
 const auto prior=read_weight_receipt(root,predecessor);
 require(revisable(prior)&&prior.id!=prepared.id&&same_clock_target(prior,prepared)&&prior.image==prepared.image&&
         same_registry(prior.before,prepared.before)&&same_clock_edits(prior.clocks,prepared.previous_clocks),
         "Previous clock session no longer matches the canceled revision");
 atomic_write(path,pointer(prior).dump(),true);
}
void save_weight_receipt(const std::wstring& dir,const PowerWeightReceipt& r){const auto bytes=encode_weight_receipt(r);const auto root=directory(dir,true);StoreLock lock(root);require(pointer_id(active_path(root,r.identity.slot,r.uuid),r.identity.slot,r.uuid)==std::optional<std::string>(r.id),"Weight journal is not the active receipt");const auto previous=read_weight_receipt(root,r.id);require(previous.identity.slot==r.identity.slot&&previous.uuid==r.uuid,"Weight journal identity changed");auto original=previous,updated=r;original.phase=updated.phase;original.message=updated.message;original.issued_names=updated.issued_names;original.refresh_requested=updated.refresh_requested;original.uncertain=updated.uncertain;original.restoring=updated.restoring;require(encode_weight_receipt(original)==bytes,"Weight journal immutable evidence changed");atomic_write(power_weight_receipt_path(root,r.id),bytes,true);}
std::optional<PowerWeightReceipt> discover_weight_receipt(const std::wstring& dir,const std::string& slot,const std::string& uuid){const auto root=directory(dir,false);if(GetFileAttributesW(root.c_str())==INVALID_FILE_ATTRIBUTES){const auto e=GetLastError();if(e==ERROR_FILE_NOT_FOUND||e==ERROR_PATH_NOT_FOUND)return {};win(false,"Inspect weight store");}StoreLock lock(root);const auto id=pointer_id(active_path(root,slot,uuid),slot,uuid);if(!id)return {};auto r=read_weight_receipt(root,*id);require(r.identity.slot==slot&&r.uuid==uuid,"Active weight receipt target mismatch");return r;}
std::vector<PowerWeightReceipt> active_clock_receipts(const std::wstring& dir,const std::string& slot) {
 const auto root=directory(dir,false);
 std::vector<PowerWeightReceipt> found;
 if (!std::filesystem::exists(root)) return found;
 for (const auto& entry : std::filesystem::directory_iterator(root)) {
  const auto name=entry.path().filename().wstring();
  static const std::wregex pattern(L"active-[0-9a-f]{64}\\.json");
  if (!std::regex_match(name,pattern)) continue;
  const auto bytes=read_bytes(entry.path().wstring());
  if (!bytes) continue;
  const auto pointer=parse(*bytes);
  const auto stored_slot=string(pointer.at("slot"),23);
  if (!slot.empty() && stored_slot!=slot) continue;
  const auto uuid=string(pointer.at("uuid"),160);
  require(active_path(root,stored_slot,uuid)==entry.path().wstring(),"Clock recovery pointer filename differs from identity");
  auto receipt=discover_weight_receipt(root,stored_slot,uuid);
  if (receipt && receipt->is_pascal_clocks()) found.push_back(std::move(*receipt));
 }
 return found;
}
bool clear_active_weight_receipt(const std::wstring& dir,const PowerWeightReceipt& r){derive_weight_receipt(r);require(r.phase=="removed"&&!r.uncertain,"Only verified removal may clear active weight receipt");const auto root=directory(dir,false);StoreLock lock(root);const auto path=active_path(root,r.identity.slot,r.uuid);const auto id=pointer_id(path,r.identity.slot,r.uuid);if(!id)return false;require(*id==r.id,"Refusing to clear another active weight receipt");const auto saved=read_weight_receipt(root,r.id);require(encode_weight_receipt(saved)==encode_weight_receipt(r),"Persist completed removal before clearing active pointer");win(DeleteFileW(path.c_str())!=0,"Clear active weight pointer");return true;}
}
