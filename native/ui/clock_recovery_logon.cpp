// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "clock_recovery_logon.h"
#include "../src/core/sha256.h"
#include <windows.h>
#include <aclapi.h>
#include <sddl.h>
#include <shellapi.h>
#include <algorithm>
#include <filesystem>
#include <memory>
#include <type_traits>
#include <stdexcept>

namespace druta::ui {
namespace {
using Path=std::filesystem::path;
constexpr auto run_key=L"Software\\Microsoft\\Windows\\CurrentVersion\\RunOnce";
constexpr auto folder_name=L"Druta Clock Recovery";
constexpr auto exe_name=L"druta-clock-recovery.exe";
constexpr size_t exe_bound=64u*1024u*1024u;

void require(bool ok,const char* reason){if(!ok)throw std::runtime_error(reason);}
struct CloseHandleDeleter {void operator()(void* h)const{if(h&&h!=INVALID_HANDLE_VALUE)CloseHandle(h);}};
using Handle=std::unique_ptr<void,CloseHandleDeleter>;
struct FreeLocal {void operator()(void* p)const{if(p)LocalFree(p);}};
using Allocation=std::unique_ptr<void,FreeLocal>;
struct CloseKey {void operator()(HKEY key)const{RegCloseKey(key);}};
using Key=std::unique_ptr<std::remove_pointer_t<HKEY>,CloseKey>;

bool valid_id(const std::string& id) {
 return id.size()==32&&std::all_of(id.begin(),id.end(),[](char c){return (c>='0'&&c<='9')||(c>='a'&&c<='f');});
}
std::wstring wide(const std::string& text){
 if(text.empty())return {};
 const auto count=static_cast<int>(text.size());
 const auto n=MultiByteToWideChar(CP_UTF8,0,text.data(),count,nullptr,0);
 if(!n)return L"Windows could not display the recovery message.";
 std::wstring result(n,L'\0');
 MultiByteToWideChar(CP_UTF8,0,text.data(),count,result.data(),n);
 return result;
}
std::wstring sid_text(PSID sid) {
 LPWSTR value=nullptr;
 require(sid&&ConvertSidToStringSidW(sid,&value),"Cannot identify Windows account");
 Allocation free(value);return value;
}
std::wstring user_sid() {
 HANDLE raw=nullptr;
 require(OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&raw)!=FALSE,"Cannot read Windows account");
 Handle token(raw);DWORD n=0;GetTokenInformation(raw,TokenUser,nullptr,0,&n);
 require(n&&n<65536,"Invalid Windows account information");
 std::vector<BYTE> buf(n);
 require(GetTokenInformation(raw,TokenUser,buf.data(),n,&n)!=FALSE,"Cannot read Windows account");
 return sid_text(reinterpret_cast<TOKEN_USER*>(buf.data())->User.Sid);
}
bool admin() {
 BYTE buf[SECURITY_MAX_SID_SIZE];DWORD size=sizeof(buf);BOOL member=FALSE;
 return CreateWellKnownSid(WinBuiltinAdministratorsSid,nullptr,buf,&size)&&CheckTokenMembership(nullptr,buf,&member)&&member;
}
struct Descriptor {
 Allocation storage;
 SECURITY_ATTRIBUTES attrs{};
 explicit Descriptor(const std::wstring& sddl) {
  PSECURITY_DESCRIPTOR raw=nullptr;
  require(ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl.c_str(),SDDL_REVISION_1,&raw,nullptr)!=FALSE,
          "Cannot prepare protected recovery permissions");
  storage.reset(raw);attrs={sizeof(attrs),raw,FALSE};
 }
};
bool trusted_admin_sid(PSID sid) {
 if(IsWellKnownSid(sid,WinLocalSystemSid)||IsWellKnownSid(sid,WinBuiltinAdministratorsSid))return true;
 return sid_text(sid)==L"S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"; // TrustedInstaller
}
// Pin each ancestor without FILE_SHARE_DELETE until this operation completes.
// A writable parent could otherwise substitute a child even with a protected
// child DACL. System volume roots commonly permit creating new folders.
void verify_handle(HANDLE h,bool directory,bool ancestor=false) {
 BY_HANDLE_FILE_INFORMATION info{};
 require(GetFileInformationByHandle(h,&info)&&!(info.dwFileAttributes&FILE_ATTRIBUTE_REPARSE_POINT)&&
         bool(info.dwFileAttributes&FILE_ATTRIBUTE_DIRECTORY)==directory,"Recovery path is not a regular file or directory");
 PSID owner=nullptr;PACL acl=nullptr;PSECURITY_DESCRIPTOR raw=nullptr;
 const auto result=GetSecurityInfo(h,SE_FILE_OBJECT,OWNER_SECURITY_INFORMATION|DACL_SECURITY_INFORMATION,
                                   &owner,nullptr,&acl,nullptr,&raw);
 Allocation sd(raw);
 require(result==ERROR_SUCCESS&&owner&&acl&&trusted_admin_sid(owner),"Recovery path must be owned by Administrators, SYSTEM or TrustedInstaller");
 const DWORD writable=DELETE|WRITE_DAC|WRITE_OWNER|FILE_DELETE_CHILD|GENERIC_WRITE|GENERIC_ALL|
                      FILE_WRITE_ATTRIBUTES|FILE_WRITE_EA|(ancestor?0u:DWORD(FILE_WRITE_DATA|FILE_APPEND_DATA));
 for(DWORD i=0;i<acl->AceCount;++i) {
  void* ptr=nullptr;require(GetAce(acl,i,&ptr)!=FALSE,"Cannot inspect recovery permissions");
  const auto* ace=static_cast<const ACCESS_ALLOWED_ACE*>(ptr);
  if(ace->Header.AceFlags&INHERIT_ONLY_ACE)continue;
  if(ace->Header.AceType==ACCESS_DENIED_ACE_TYPE)continue;
  require(ace->Header.AceType==ACCESS_ALLOWED_ACE_TYPE,"Recovery path has unsupported access control entries");
  if(ace->Mask&writable)require(trusted_admin_sid(const_cast<DWORD*>(&ace->SidStart)),
                                "Recovery path is writable by a non-administrator");
 }
}
std::wstring registry_text(HKEY key,const wchar_t* name) {
 wchar_t buf[32768]{};DWORD size=sizeof(buf),type=0;
 require(RegQueryValueExW(key,name,nullptr,&type,reinterpret_cast<BYTE*>(buf),&size)==ERROR_SUCCESS&&
         type==REG_SZ&&size>=sizeof(wchar_t)&&size<=sizeof(buf)&&!(size%sizeof(wchar_t))&&
         !buf[size/sizeof(wchar_t)-1]&&wcslen(buf)+1==size/sizeof(wchar_t),"Cannot resolve system Program Files directory");
 return buf;
}
Path program_files() {
 HKEY raw=nullptr;require(RegOpenKeyExW(HKEY_LOCAL_MACHINE,L"SOFTWARE\\Microsoft\\Windows\\CurrentVersion",0,
                                     KEY_QUERY_VALUE|KEY_WOW64_64KEY,&raw)==ERROR_SUCCESS,"Cannot open system installation path");
 Key key(raw);Path root=registry_text(raw,L"ProgramFilesDir");
 require(root.is_absolute()&&root.root_name().wstring().size()==2&&root==root.lexically_normal()&&
         GetDriveTypeW(root.root_path().c_str())==DRIVE_FIXED,"Recovery needs a local Program Files directory");
 return root;
}
Path self() {
 std::wstring text(32768,0);DWORD n=GetModuleFileNameW(nullptr,text.data(),static_cast<DWORD>(text.size()));
 require(n&&n<text.size(),"Cannot resolve the recovery executable");text.resize(n);return text;
}
void pin_path(std::vector<Handle>& pinned,const Path& path,bool ancestor) {
  Handle dir(CreateFileW(path.c_str(),READ_CONTROL|FILE_READ_ATTRIBUTES,FILE_SHARE_READ|FILE_SHARE_WRITE,
                 nullptr,OPEN_EXISTING,FILE_FLAG_BACKUP_SEMANTICS|FILE_FLAG_OPEN_REPARSE_POINT,nullptr));
  require(dir.get()!=INVALID_HANDLE_VALUE,"Cannot open protected recovery directory");
  verify_handle(dir.get(),true,ancestor);pinned.push_back(std::move(dir));
 }
void pin_ancestors(std::vector<Handle>& pinned,const Path& programs) {
  for(Path current=programs.root_path();;){
   pin_path(pinned,current,true);
   if(current==programs)break;
   auto relative=programs.lexically_relative(current);
   require(relative.begin()!=relative.end()&&*relative.begin()!=L"..","Invalid Program Files parent");
   current/=*relative.begin();
  }
}
struct AccountDirectory {
 Path folder;
 std::wstring sid;
 std::vector<Handle> pinned;
 explicit AccountDirectory(bool create) : sid(user_sid()) {
  const Path programs=program_files();
  pin_ancestors(pinned,programs);
  folder=programs/folder_name;
  create_or_pin(folder,create,L"BU");
  folder/=sid;
  create_or_pin(folder,create,sid);
 }
 Path marker_path(const std::string& id) const {
  require(valid_id(id),"Invalid clock recovery receipt ID");
  return folder/(wide(id)+L".json");
 }
 void create_or_pin(const Path& p,bool create,const std::wstring& reader) {
  if(create) {
   Descriptor acl(L"O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;GRGX;;;"+reader+L")");
   require(CreateDirectoryW(p.c_str(),&acl.attrs)||GetLastError()==ERROR_ALREADY_EXISTS,"Cannot create protected recovery directory");
  }
  pin_path(pinned,p,false);
 }
};
struct Location : AccountDirectory {
 Path exe,marker;
 std::wstring value,command;
 explicit Location(const std::string& id,bool create) : AccountDirectory(create) {
  exe=folder/exe_name;
  marker=marker_path(id);
  value=L"*DrutaClockRecovery-"+wide(id);
  command=L"\""+exe.wstring()+L"\" --clock-recovery-logon "+wide(id);
  require(command.size()<260,"Recovery RunOnce command exceeds the Windows limit");
 }
};
std::string read_file(const Path& path,size_t maximum,bool protect) {
 Handle h(CreateFileW(path.c_str(),GENERIC_READ|(protect?READ_CONTROL:0),FILE_SHARE_READ,nullptr,OPEN_EXISTING,
                      FILE_FLAG_OPEN_REPARSE_POINT,nullptr));
 require(h.get()!=INVALID_HANDLE_VALUE,"Cannot open clock recovery file");
 if(protect)verify_handle(h.get(),false);
 else {
  BY_HANDLE_FILE_INFORMATION info{};require(GetFileInformationByHandle(h.get(),&info)&&!(info.dwFileAttributes&
              (FILE_ATTRIBUTE_REPARSE_POINT|FILE_ATTRIBUTE_DIRECTORY)),"Recovery source must be a regular executable");
 }
 LARGE_INTEGER len{};require(GetFileSizeEx(h.get(),&len)&&len.QuadPart>0&&len.QuadPart<=static_cast<LONGLONG>(maximum),
                            "Invalid clock recovery file size");
 std::string text(static_cast<size_t>(len.QuadPart),0);DWORD got=0;
 require(ReadFile(h.get(),text.data(),static_cast<DWORD>(text.size()),&got,nullptr)&&got==text.size(),"Cannot read clock recovery file");
 return text;
}
bool path_present(const Path& p){
 if(GetFileAttributesW(p.c_str())!=INVALID_FILE_ATTRIBUTES)return true;
 require(GetLastError()==ERROR_FILE_NOT_FOUND||GetLastError()==ERROR_PATH_NOT_FOUND,"Cannot inspect clock recovery file");
 return false;
}
void write_new(const Path& path,const std::string& data,const std::wstring& sid) {
 Descriptor acl(L"O:BAG:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;GRGX;;;"+sid+L")");
 FILETIME stamp{};GetSystemTimeAsFileTime(&stamp);
 const auto temp=path.wstring()+L"."+std::to_wstring(GetCurrentProcessId())+L"."+
                 std::to_wstring(stamp.dwHighDateTime)+L"."+std::to_wstring(stamp.dwLowDateTime)+L".tmp";
 Handle file(CreateFileW(temp.c_str(),GENERIC_WRITE,0,&acl.attrs,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,nullptr));
 require(file.get()!=INVALID_HANDLE_VALUE,"Cannot create protected clock recovery file");
 DWORD wrote=0;const bool ok=WriteFile(file.get(),data.data(),static_cast<DWORD>(data.size()),&wrote,nullptr)&&
                  wrote==data.size()&&FlushFileBuffers(file.get());
 file.reset();
 if(!ok||!MoveFileExW(temp.c_str(),path.c_str(),MOVEFILE_WRITE_THROUGH)){
  DeleteFileW(temp.c_str());throw std::runtime_error("Cannot publish durable clock recovery file");
 }
}
std::string digest(const std::string& data) {
 return druta::sha256_hex(reinterpret_cast<const uint8_t*>(data.data()),data.size());
}
struct Enrollment { PowerWeightReceipt receipt;std::string image_hash; };
Enrollment pinned_record(const AccountDirectory& loc,const std::string& id) {
 const auto doc=ProfileJson::parse(read_file(loc.marker_path(id),2*power_weight_receipt_max_bytes,true));
 const auto sid=doc.at("sid").get<std::string>();
 require(doc.size()==4&&doc.at("format")=="druta-clock-logon-1"&&wide(sid)==loc.sid,"Clock recovery enrollment belongs to another account");
 Enrollment out{decode_weight_receipt(doc.at("receipt").get<std::string>()),doc.at("exe_sha256").get<std::string>()};
 require(out.receipt.id==id&&out.receipt.is_pascal_clocks()&&out.receipt.phase=="prepared"&&
         out.receipt.issued_names.empty()&&!out.receipt.refresh_requested&&!out.receipt.restoring&&!out.receipt.uncertain&&
         out.image_hash.size()==64&&std::all_of(out.image_hash.begin(),out.image_hash.end(),[](char c){
           return (c>='0'&&c<='9')||(c>='a'&&c<='f');
         }),"Invalid clock recovery enrollment");
 return out;
}
Key open_run_key(bool write) {
 HKEY raw=nullptr;
 const auto access=KEY_QUERY_VALUE|(write?KEY_SET_VALUE:0);
 const auto status=write?RegCreateKeyExW(HKEY_CURRENT_USER,run_key,0,nullptr,0,access,nullptr,&raw,nullptr):
                         RegOpenKeyExW(HKEY_CURRENT_USER,run_key,0,access,&raw);
 if(!write&&status==ERROR_FILE_NOT_FOUND)return {};
 require(status==ERROR_SUCCESS,"Cannot access this user's RunOnce key");return Key(raw);
}
bool registered(HKEY key,const Location& loc) {
 if(!key)return false;
 DWORD type=0,size=0;const auto status=RegQueryValueExW(key,loc.value.c_str(),nullptr,&type,nullptr,&size);
 if(status==ERROR_FILE_NOT_FOUND)return false;
 require(status==ERROR_SUCCESS&&type==REG_SZ&&size==(loc.command.size()+1)*sizeof(wchar_t),
         "A different RunOnce entry already owns this receipt name");
 std::wstring cmd(size/sizeof(wchar_t),0);
 require(RegQueryValueExW(key,loc.value.c_str(),nullptr,&type,reinterpret_cast<BYTE*>(cmd.data()),&size)==ERROR_SUCCESS&&
         cmd==loc.command+L'\0',"RunOnce command changed");
 return true;
}
}
namespace clock_logon_test {
void check_system_directory_permissions() {
 std::vector<Handle> pins;
 pin_ancestors(pins,program_files());
}
}
bool clock_logon_receipts_match(const PowerWeightReceipt& pinned,const PowerWeightReceipt& current) {
 try {
  if(!pinned.is_pascal_clocks()||!current.is_pascal_clocks())return false;
  // Progress fields may advance; every issued registry name must still be
  // derivable from the pinned request and captured original image.
  (void)derive_weight_receipt(current);
  auto fixed=current;fixed.phase=pinned.phase;fixed.message=pinned.message;fixed.issued_names=pinned.issued_names;
  fixed.refresh_requested=pinned.refresh_requested;fixed.uncertain=pinned.uncertain;fixed.restoring=pinned.restoring;
  return encode_weight_receipt(pinned)==encode_weight_receipt(fixed);
 }catch(...){return false;}
}
namespace {
Handle lock_location(const Location& loc) {
 Descriptor acl(L"O:BAG:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)");
 Handle lock(CreateFileW((loc.folder/L"enrollment.lock").c_str(),GENERIC_WRITE|READ_CONTROL,0,&acl.attrs,
                         OPEN_ALWAYS,FILE_ATTRIBUTE_NORMAL|FILE_FLAG_OPEN_REPARSE_POINT,nullptr));
 require(lock.get()!=INVALID_HANDLE_VALUE,"Clock recovery enrollment is busy or inaccessible");
 verify_handle(lock.get(),false);return lock;
}
void stage_executable(const Location& loc,const std::string& contents,const std::string& hash) {
 if(!path_present(loc.exe)){write_new(loc.exe,contents,loc.sid);return;}
 if(digest(read_file(loc.exe,exe_bound,true))==hash)return;
 for(const auto& entry:std::filesystem::directory_iterator(loc.folder))
  require(entry.path().extension()!=L".json","Finish the previously armed clock recovery before updating its protected executable");
 const Path temp=loc.folder/(L"update-"+std::to_wstring(GetCurrentProcessId())+L".tmp");
 write_new(temp,contents,loc.sid);
 if(!MoveFileExW(temp.c_str(),loc.exe.c_str(),MOVEFILE_REPLACE_EXISTING|MOVEFILE_WRITE_THROUGH)){
  DeleteFileW(temp.c_str());throw std::runtime_error("Cannot update the protected recovery executable");
 }
}
bool rollback_runonce(HKEY key,const Location& loc) {
 if(!registered(key,loc))return true;
 return RegDeleteValueW(key,loc.value.c_str())==ERROR_SUCCESS && RegFlushKey(key)==ERROR_SUCCESS;
}
}
bool arm_clock_logon_recovery(const PowerWeightReceipt& r,std::string& error) {
 try{
  require(admin(),"Run the recovery setup as administrator under the account that will sign in");
  require(r.is_pascal_clocks()&&r.phase=="prepared"&&r.issued_names.empty()&&!r.refresh_requested&&
          !r.restoring&&!r.uncertain,"Arm logon recovery before staging the reviewed clock edits");
  const auto serialized=encode_weight_receipt(r);
  Location loc(r.id,true);auto lock=lock_location(loc);
  auto key=open_run_key(true);const bool prior=registered(key.get(),loc);
  const auto bytes=read_file(self(),exe_bound,false);const auto hash=digest(bytes);
  const bool pinned=path_present(loc.marker);
  require(!prior||pinned,"An existing clock recovery command lacks its protected receipt");
  if(pinned) {
   const auto old=pinned_record(loc,r.id);
   require(clock_logon_receipts_match(old.receipt,r)&&old.image_hash==hash,"A different recovery enrollment owns this receipt");
  }
  stage_executable(loc,bytes,hash);
  require(digest(read_file(loc.exe,exe_bound,true))==hash,"Protected clock recovery executable readback differs");
  if(prior)return true;
  if(!pinned){
   std::string sid;for(const auto c:loc.sid)sid.push_back(static_cast<char>(c));
   write_new(loc.marker,ProfileJson{{"format","druta-clock-logon-1"},{"sid",sid},{"receipt",serialized},
                                    {"exe_sha256",hash}}.dump(),loc.sid);
  }
  bool issued=false;
  try{
   require(clock_logon_receipts_match(pinned_record(loc,r.id).receipt,r),"Protected clock receipt readback differs");
   require(RegSetValueExW(key.get(),loc.value.c_str(),0,REG_SZ,reinterpret_cast<const BYTE*>(loc.command.c_str()),
                         static_cast<DWORD>((loc.command.size()+1)*sizeof(wchar_t)))==ERROR_SUCCESS,
           "Cannot register clock recovery for this user");
   issued=true;
   require(RegFlushKey(key.get())==ERROR_SUCCESS&&registered(key.get(),loc),"Cannot verify durable clock recovery registration");
  }catch(...){
   if((!issued||rollback_runonce(key.get(),loc))&&!pinned)DeleteFileW(loc.marker.c_str());
   throw;
  }
  return true;
 }catch(const std::exception& e){error=e.what();return false;}
}

bool disarm_clock_logon_recovery(const PowerWeightReceipt& r,std::string& error) {
 try{
  require(admin(),"Run clock recovery cleanup as administrator under the original account");
  require(valid_id(r.id)&&r.is_pascal_clocks(),"Invalid clock recovery target");
  // A never-armed package needs no setup or registry key creation.
  if(!path_present(program_files()/folder_name/user_sid()))return true;
  Location loc(r.id,false);auto lock=lock_location(loc);
  auto existing=open_run_key(false);
  const bool ours=registered(existing.get(),loc);
  if(path_present(loc.marker))require(clock_logon_receipts_match(pinned_record(loc,r.id).receipt,r),
                                "Protected clock receipt differs from this recovery request");
  else {require(!ours,"Orphaned clock recovery command has no protected receipt");return true;}
  if(ours){auto key=open_run_key(true);require(rollback_runonce(key.get(),loc),"Could not remove this receipt's RunOnce entry");}
  require(DeleteFileW(loc.marker.c_str())!=0,"Could not remove this receipt's protected enrollment");
  return true;
 }catch(const std::exception& e){error=e.what();return false;}
}
bool clock_logon_recovery_enrolled(const PowerWeightReceipt& r) {
 try{Location loc(r.id,false);return clock_logon_receipts_match(pinned_record(loc,r.id).receipt,r);}
 catch(...){return false;}
}
std::vector<PowerWeightReceipt> clock_logon_recovery_enrollments() {
 std::vector<PowerWeightReceipt> records;
 if(!path_present(program_files()/folder_name/user_sid()))return records;
 AccountDirectory dir(false);
 for(const auto& entry:std::filesystem::directory_iterator(dir.folder)) {
  const auto filename=entry.path().filename().wstring();
  if(filename.size()!=37||filename.substr(32)!=L".json")continue;
  const auto stem=filename.substr(0,32);
  if(!std::all_of(stem.begin(),stem.end(),[](wchar_t c){return (c>=L'0'&&c<=L'9')||(c>=L'a'&&c<=L'f');}))continue;
  std::string id;
  for(const auto c:stem)id.push_back(static_cast<char>(c));
  try {records.push_back(pinned_record(dir,id).receipt);}
  catch(const std::exception&) {
   // Another process may have disarmed this record during enumeration.
   // Surface damaged evidence rather than report that the account is clean.
   if(path_present(dir.marker_path(id)))throw;
  }
 }
 std::sort(records.begin(),records.end(),[](const auto& a,const auto& b){return a.id<b.id;});
 return records;
}
bool clock_logon_recovery_armed(const PowerWeightReceipt& r) {
 try{
  Location loc(r.id,false);const auto pin=pinned_record(loc,r.id);
  return clock_logon_receipts_match(pin.receipt,r)&&digest(read_file(loc.exe,exe_bound,true))==pin.image_hash&&
         registered(open_run_key(false).get(),loc);
 }catch(...){return false;}
}
int clock_logon_test::run(const SignIn& session,const std::string& id,
    const std::function<PowerWeightTransactionResult(const PowerWeightReceipt&)>& recover) {
 std::wstring retry;
 if(valid_id(id)) {
  retry=L"\n\nTo retry, sign in to the Windows account that prepared this clock edit. In Safe Mode, use the account password. "
        L"From an administrator Command Prompt in the Druta folder, run:\n"
        L"druta-ui.exe --recover-clock-overrides "+wide(id);
 }
 try {
  require(valid_id(id)&&bool(recover),"Invalid clock recovery callback");
  Location loc(id,false);
  require(_wcsicmp(session.executable().c_str(),loc.exe.c_str())==0,"Run the protected Druta recovery copy for this enrollment");
  const auto pin=pinned_record(loc,id);
  require(digest(read_file(loc.exe,exe_bound,true))==pin.image_hash,"Protected clock recovery executable differs from its enrollment");
  retry=L"\n\nTo retry, open an administrator Command Prompt in the Windows account that prepared this clock edit and run:\n\""+
        loc.exe.wstring()+L"\" --recover-clock-overrides "+wide(id)+
        L"\nIn Safe Mode, sign in with the account password.";
  if(!session.safe_mode) {
   const auto message=L"Windows started normally. The automatic clock recovery runs only in Safe Mode. "
                      L"You can recover this clock edit manually now."+retry;
   session.prompt(message,MB_OK|MB_ICONINFORMATION);
   return 1;
  }
  if(!session.elevated){
   const auto prompt=L"Restore the staged clock settings for "+wide(pin.receipt.identity.slot)+
                     L" and clear its one-time recovery? Windows will ask for administrator approval."+retry;
   if(session.prompt(prompt,MB_YESNO|MB_DEFBUTTON2|MB_ICONQUESTION)!=IDYES)return 1;
   require(session.elevate(loc.exe.wstring(),L"--clock-recovery-logon "+wide(id)),
           "Windows administrator approval was declined or unavailable.");
   return 0;
  }
  const auto result=recover(pin.receipt);
  const auto message=wide(result.message)+(result.ok?std::wstring():retry);
  session.prompt(message,MB_OK|(result.ok?MB_ICONINFORMATION:MB_ICONERROR));
  return result.ok?0:1;
 }catch(const std::exception& e){
  const auto message=wide(e.what())+retry;
  session.prompt(message,MB_OK|MB_ICONERROR);return 1;
 }
}
int run_clock_logon_recovery(const std::string& id,
    const std::function<PowerWeightTransactionResult(const PowerWeightReceipt&)>& recover) {
 clock_logon_test::SignIn session;
 session.executable=[] {return self().wstring();};
 session.safe_mode=GetSystemMetrics(SM_CLEANBOOT)!=0;
 session.elevated=admin();
 session.prompt=[](const std::wstring& text,unsigned flags) {
  return MessageBoxW(nullptr,text.c_str(),L"Druta clock recovery",flags);
 };
 session.elevate=[](const std::wstring& exe,const std::wstring& args) {
  SHELLEXECUTEINFOW launch{};launch.cbSize=sizeof(launch);launch.lpVerb=L"runas";
  launch.lpFile=exe.c_str();launch.lpParameters=args.c_str();
  const auto directory=Path(exe).parent_path();launch.lpDirectory=directory.c_str();launch.nShow=SW_SHOWNORMAL;
  return ShellExecuteExW(&launch)!=FALSE;
 };
 return clock_logon_test::run(session,id,recover);
}
}  // namespace druta::ui
