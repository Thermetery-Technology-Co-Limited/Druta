// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "clock_recovery_boot_bcd.h"
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <cstdio>
#include <filesystem>
#include <stdexcept>
#include <vector>

namespace {
using namespace druta::ui::clock_boot_test;
constexpr wchar_t source[] = L"{09eba312-608c-4b0a-88c1-57661962e2b1}";
constexpr char receipt[] = "a98ac271847f4d63bf47d8db785f05af";
constexpr char other[] = "89f07720d8c142d897453155ba0598e3";

void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}
template<class Run> void must_refuse(Run action) {
    bool refused = false;
    try { action(); } catch (const std::exception&) { refused = true; }
    require(refused, "Expected operation to decline");
}

struct PrivateBcd {
    std::wstring dir, file;
    PrivateBcd() {
        wchar_t temp[MAX_PATH]{}, name[MAX_PATH]{};
        require(GetTempPathW(MAX_PATH,temp) && GetTempFileNameW(temp,L"DBC",0,name), "Cannot allocate a test directory");
        require(DeleteFileW(name) && CreateDirectoryW(name,nullptr), "Cannot create test directory");
        dir=name;
        file=dir+L"\\test.bcd";
    }
    ~PrivateBcd() { std::error_code error; std::filesystem::remove_all(dir,error); }

    void invoke(const std::wstring& options, bool create = false) {
        wchar_t sys[MAX_PATH]{};
        require(GetSystemDirectoryW(sys,MAX_PATH) != 0, "System directory missing");
        const auto exe = std::wstring(sys) + L"\\bcdedit.exe";
        auto command = L"\"" + exe + (create ? L"\" /createstore \"" : L"\" /store \"") + file + L"\" " + options;
        STARTUPINFOW start{}; start.cb=sizeof(start);
        PROCESS_INFORMATION pi{};
        require(CreateProcessW(exe.c_str(),command.data(),nullptr,nullptr,FALSE,CREATE_NO_WINDOW,nullptr,sys,&start,&pi)!=0,
                "Cannot launch fixture BCDEdit");
        CloseHandle(pi.hThread);
        DWORD code=1;
        const bool finished=WaitForSingleObject(pi.hProcess,60000)==WAIT_OBJECT_0 && GetExitCodeProcess(pi.hProcess,&code);
        CloseHandle(pi.hProcess);
        require(finished && code==0, "BCDEdit rejected fixture preparation");
    }

    std::wstring clone() const {
        using OpenHive=LSTATUS(WINAPI*)(LPCWSTR,PHKEY,REGSAM,DWORD,DWORD);
        const auto open=reinterpret_cast<OpenHive>(GetProcAddress(GetModuleHandleW(L"advapi32.dll"),"RegLoadAppKeyW"));
        HKEY root=nullptr,objects=nullptr;
        require(open && open(file.c_str(),&root,KEY_READ,0,0)==ERROR_SUCCESS,"Cannot read private fixture");
        require(RegOpenKeyExW(root,L"Objects",0,KEY_READ,&objects)==ERROR_SUCCESS,"Cannot inspect fixture");
        std::wstring answer;
        for(DWORD i=0;;++i) {
            wchar_t id[128]{}; DWORD chars=128;
            if(RegEnumKeyExW(objects,i,id,&chars,nullptr,nullptr,nullptr,nullptr)!=ERROR_SUCCESS)break;
            if(_wcsicmp(id,source)&&_wcsicmp(id,L"{9dea862c-5cdd-4e70-acc1-f32b344d4795}")) answer=id;
        }
        RegCloseKey(objects);RegCloseKey(root);return answer;
    }
};

void fixture() {
    PrivateBcd b;
    b.invoke(L"",true);
    b.invoke(L"/create {bootmgr} /d Manager");
    b.invoke(std::wstring(L"/create ")+source+L" /d Normal /application osloader");
    b.invoke(std::wstring(L"/default ")+source);
    b.invoke(std::wstring(L"/displayorder ")+source+L" /addlast");
    require(!armed_file(b.file,receipt),"Fresh store already armed");
    must_refuse([&]{arm_file(b.file,source,"invalid");});
    disarm_file(b.file,receipt);

    b.invoke(std::wstring(L"/bootsequence ")+source+L" /addfirst");
    must_refuse([&]{arm_file(b.file,source,receipt);});
    require(b.clone().empty(),"Unrelated one-shot choice caused a partial clone");
    b.invoke(std::wstring(L"/bootsequence ")+source+L" /remove");

    b.invoke(L"/set {bootmgr} persistbootsequence yes");
    must_refuse([&]{arm_file(b.file,source,receipt);});
    b.invoke(L"/deletevalue {bootmgr} persistbootsequence");

    b.invoke(std::wstring(L"/copy ")+source+L" /d \"Druta clock recovery "+std::wstring(receipt,receipt+32)+L"\"");
    const auto copy=b.clone();
    require(!copy.empty()&&!armed_file(b.file,receipt),"Partial copied loader is not armed");
    arm_file(b.file,source,receipt);
    require(armed_file(b.file,receipt)&&copy==b.clone(),"Cannot resume partial arm");
    arm_file(b.file,source,receipt);
    must_refuse([&]{arm_file(b.file,source,other);});
    require(armed_file(b.file,receipt),"Racing receipt replaced first choice");

    b.invoke(std::wstring(L"/default ")+copy);
    must_refuse([&]{disarm_file(b.file,receipt);});
    require(b.clone()==copy,"Foreign default was deleted");
    b.invoke(std::wstring(L"/default ")+source);

    b.invoke(std::wstring(L"/bootsequence ")+source+L" /addfirst");
    require(!armed_file(b.file,receipt),"An altered boot order still advertised protection");
    disarm_file(b.file,receipt);
    require(b.clone().empty(),"Retiring recovery did not remove its loader");
    must_refuse([&]{arm_file(b.file,source,receipt);}); // the foreign one-time selection survived
    b.invoke(std::wstring(L"/bootsequence ")+source+L" /remove");
    arm_file(b.file,source,receipt);
    b.invoke(std::wstring(L"/bootsequence ")+b.clone()+L" /remove"); // simulate bootmgr consuming it
    require(!armed_file(b.file,receipt),"Consumed selection advertised protection");
    disarm_file(b.file,receipt);
    disarm_file(b.file,receipt);
    require(b.clone().empty(),"Final cleanup retained the copied loader");
}
}

int main() {
    BYTE sid[SECURITY_MAX_SID_SIZE]; DWORD length=sizeof(sid); BOOL admin=FALSE;
    if (!druta::ui::clock_recovery_boot_supported() ||
        !CreateWellKnownSid(WinBuiltinAdministratorsSid,nullptr,sid,&length) ||
        !CheckTokenMembership(nullptr,sid,&admin) || !admin) {
        std::puts("Isolated BCD fixture requires Vista or later and an elevated test process.");
        return 77;
    }
    try { fixture(); std::puts("One-time boot tests passed using an isolated BCD file."); return 0; }
    catch(const std::exception& error){std::fprintf(stderr,"%s\n",error.what());return 1;}
}
