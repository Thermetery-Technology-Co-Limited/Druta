/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "timing_transport.h"
#include <windows.h>
#include <wincrypt.h>
#pragma comment(lib, "advapi32.lib")
#include <regex>
#include <sstream>
#include <vector>
namespace druta::ui {
namespace {
class NativeTransport final : public TimingTransport {
    std::wstring path_; HANDLE file_=INVALID_HANDLE_VALUE; std::string token_;
    mutable HANDLE pending_process_=nullptr;
    static bool digest(HANDLE file,std::string& out) {
        HCRYPTPROV provider=0; HCRYPTHASH hash=0; bool ok=false;
        if (!CryptAcquireContextW(&provider,nullptr,nullptr,PROV_RSA_AES,CRYPT_VERIFYCONTEXT)) return false;
        if (CryptCreateHash(provider,CALG_SHA_256,0,0,&hash)) {
            LARGE_INTEGER zero{};
            if (SetFilePointerEx(file,zero,nullptr,FILE_BEGIN)) {
                uint8_t buf[32768]; DWORD n=0; uint64_t total=0; bool read_ok=true;
                do { if (!ReadFile(file,buf,sizeof(buf),&n,nullptr)) { read_ok=false; break; }
                     total+=n; if (total>256u*1024u*1024u || (n && !CryptHashData(hash,buf,n,0))) { read_ok=false; break; }
                } while(n);
                BYTE bytes[32]{}; DWORD size=sizeof(bytes);
                if (read_ok && CryptGetHashParam(hash,HP_HASHVAL,bytes,&size,0) && size==32) {
                    static const char hex[]="0123456789abcdef"; out.clear(); out.reserve(64);
                    for (BYTE b:bytes) {out.push_back(hex[b>>4]);out.push_back(hex[b&15]);} ok=true;
                }
            }
            CryptDestroyHash(hash);
        }
        CryptReleaseContext(provider,0); return ok;
    }    static std::string file_token(const BY_HANDLE_FILE_INFORMATION& f) {
        std::ostringstream o; o<<f.dwVolumeSerialNumber<<':'<<f.nFileIndexHigh<<':'<<f.nFileIndexLow<<':'
            <<f.nFileSizeHigh<<':'<<f.nFileSizeLow<<':'<<f.ftLastWriteTime.dwHighDateTime<<':'<<f.ftLastWriteTime.dwLowDateTime;
        return o.str();
    }
public:
    explicit NativeTransport(std::wstring path) : path_(std::move(path)) {}
    ~NativeTransport() override { unpin(); if (pending_process_) CloseHandle(pending_process_); }
    bool pending() const override {
        if (!pending_process_) return false;
        if (WaitForSingleObject(pending_process_,0)!=WAIT_OBJECT_0) return true;
        CloseHandle(pending_process_); pending_process_=nullptr; return false;
    }
    bool pin(std::string& error) override {
        if (pending()) { error="Previous helper process has not exited"; return false; }
        unpin();
        const bool absolute=path_.size()>=3 && ((path_[1]==L':' && (path_[2]==L'\\' || path_[2]==L'/')) || (path_[0]==L'\\' && path_[1]==L'\\'));
        if (!absolute || path_.find(L'"')!=std::wstring::npos) { error="An absolute helper executable path is required"; return false; }
        file_=CreateFileW(path_.c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr);
        BY_HANDLE_FILE_INFORMATION info{};
        if (file_==INVALID_HANDLE_VALUE || !GetFileInformationByHandle(file_,&info) || (info.dwFileAttributes&FILE_ATTRIBUTE_DIRECTORY)) {
            error="Could not pin the helper executable against modification"; unpin(); return false;
        }
        std::string hash; if (!digest(file_,hash)) { error="Could not fingerprint the helper"; unpin(); return false; }
        token_=file_token(info)+":"+hash; return true;
    }
    bool unchanged(std::string& error) override {
        if (file_==INVALID_HANDLE_VALUE) { error="Helper is not pinned"; return false; }
        HANDLE current=CreateFileW(path_.c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr);
        BY_HANDLE_FILE_INFORMATION info{}; const bool ok=current!=INVALID_HANDLE_VALUE && GetFileInformationByHandle(current,&info) && token_.rfind(file_token(info)+":",0)==0;
        if (current!=INVALID_HANDLE_VALUE) CloseHandle(current);
        if (!ok) error="Helper path or file changed"; return ok;
    }
    std::string token() const override { return token_; }
    void unpin() override { if (file_!=INVALID_HANDLE_VALUE) CloseHandle(file_); file_=INVALID_HANDLE_VALUE; token_.clear(); }
    TimingProcessResult run(TimingCommand op,const std::string& slot,const std::string& field,uint32_t value,bool commit_flag,bool preview_flag) override {
        TimingProcessResult result; std::string error;
        if (pending()) { result.error="Previous helper process has not exited"; return result; }
        if (!unchanged(error)) { result.error=error; return result; }
        static const std::regex slot_re("^[0-9A-Fa-f]{4}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}\\.[0-7]$");
        static const std::regex field_re("^[A-Za-z][A-Za-z0-9_]{0,63}$");
        if (op!=TimingCommand::help && op!=TimingCommand::fields && !std::regex_match(slot,slot_re)) { result.error="Invalid selected PCI slot"; return result; }
        if ((op==TimingCommand::preview || op==TimingCommand::commit) && !std::regex_match(field,field_re)) { result.error="Invalid timing field"; return result; }
        struct Temp { std::wstring path; ~Temp(){if(!path.empty())DeleteFileW(path.c_str());} } temp;
        if(op==TimingCommand::save) {
            wchar_t directory[MAX_PATH+1]{}, filename[MAX_PATH+1]{};
            if(!GetTempPathW(MAX_PATH,directory) || !GetTempFileNameW(directory,L"dtu",0,filename)) { result.error="Could not create temporary snapshot path";return result; }
            temp.path=filename;
            if(!DeleteFileW(temp.path.c_str())) { result.error="Could not prepare temporary snapshot path";return result; }
        }
        std::wstring args=L"\""+path_+L"\" ";
        if (op==TimingCommand::help) args+=L"--help";
        else if (op==TimingCommand::fields) args+=L"fields";
        else {
            args+=(op==TimingCommand::dump ? L"dump" : op==TimingCommand::save ? L"save" : L"set"); args+=L" -d "+std::wstring(slot.begin(),slot.end());
            if (op==TimingCommand::dump) args+=L" --raw";
            else if(op==TimingCommand::save) args+=L" -o \""+temp.path+L"\"";
            else { args+=L" "+std::wstring(field.begin(),field.end())+L"="+std::to_wstring(value);
                   if (op==TimingCommand::preview && preview_flag) args+=L" --dry-run";
                   if (op==TimingCommand::commit && commit_flag) args+=L" --commit"; }
        }
        SECURITY_ATTRIBUTES sa{sizeof(sa),nullptr,TRUE}; HANDLE rd=nullptr,wr=nullptr;
        if (!CreatePipe(&rd,&wr,&sa,0)) { result.error="Could not create helper output pipe"; return result; }
        SetHandleInformation(rd,HANDLE_FLAG_INHERIT,0);
        STARTUPINFOW si{}; si.cb=sizeof(si); si.dwFlags=STARTF_USESTDHANDLES; si.hStdOutput=wr; si.hStdError=wr; si.hStdInput=GetStdHandle(STD_INPUT_HANDLE);
        PROCESS_INFORMATION pi{}; std::vector<wchar_t> cmd(args.begin(),args.end()); cmd.push_back(0);
        if (!CreateProcessW(path_.c_str(),cmd.data(),nullptr,nullptr,TRUE,CREATE_NO_WINDOW,nullptr,nullptr,&si,&pi)) {
            CloseHandle(wr); CloseHandle(rd); result.error="Could not start selected helper"; return result;
        }
        result.launched=true; CloseHandle(wr); const ULONGLONG start=GetTickCount64();
        bool running=true, failed=false;
        while (running) {
            DWORD available=0; if (!PeekNamedPipe(rd,nullptr,0,nullptr,&available,nullptr)) available=0;
            if (available) {
                char buf[4096]; DWORD got=0; const DWORD count=available<sizeof(buf)?available:static_cast<DWORD>(sizeof(buf));
                if (ReadFile(rd,buf,count,&got,nullptr) && got) result.output.append(buf,got);
                if (result.output.size()>1024*1024) { result.error="Helper output exceeded limit"; failed=true; break; }
            } else if (WaitForSingleObject(pi.hProcess,20)==WAIT_OBJECT_0) {
                DWORD remaining=0;
                if (!PeekNamedPipe(rd,nullptr,0,nullptr,&remaining,nullptr) || !remaining) running=false;
            }
            if (GetTickCount64()-start>90000) { result.timed_out=true; result.error="Helper timed out"; failed=true; break; }
        }
        if (failed) {
            TerminateProcess(pi.hProcess,1);
            if (WaitForSingleObject(pi.hProcess,5000)!=WAIT_OBJECT_0) {
                pending_process_=pi.hProcess; pi.hProcess=nullptr;
                result.error+="; helper exit unconfirmed";
            }
        }
        DWORD code=1; if (pi.hProcess) GetExitCodeProcess(pi.hProcess,&code); result.exit_code=code;
        CloseHandle(pi.hThread); if (pi.hProcess) CloseHandle(pi.hProcess); CloseHandle(rd);
        if (!unchanged(error)) result.error=error;
        if(op==TimingCommand::save && result.launched && !result.timed_out && !result.exit_code && result.error.empty()) {
            HANDLE file=CreateFileW(temp.path.c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr);
            LARGE_INTEGER size{};
            if(file==INVALID_HANDLE_VALUE || !GetFileSizeEx(file,&size) || size.QuadPart<=0 || size.QuadPart>1024*1024) result.error="Snapshot file missing or exceeds limit";
            else { std::string content(static_cast<size_t>(size.QuadPart),'\0');DWORD read=0;
                if(!ReadFile(file,content.data(),static_cast<DWORD>(content.size()),&read,nullptr) || read!=content.size()) result.error="Snapshot file read failed";
                else result.output=std::move(content);
            }
            if(file!=INVALID_HANDLE_VALUE)CloseHandle(file);
        }
        return result;
    }
};
}
std::unique_ptr<TimingTransport> make_timing_transport(const std::wstring& path) { return std::make_unique<NativeTransport>(path); }
}
