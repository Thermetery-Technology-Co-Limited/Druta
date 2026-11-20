// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "timing_helper_config.h"
#include "profile_io.h"
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <filesystem>
#include <stdexcept>
#include <vector>
namespace druta::ui {
namespace {
std::wstring env(const wchar_t* name){DWORD n=GetEnvironmentVariableW(name,nullptr,0);if(!n||n>32767)return {};std::vector<wchar_t> value(n);const auto got=GetEnvironmentVariableW(name,value.data(),n);return got&&got<n?std::wstring(value.data(),got):L"";}
std::wstring wide(const std::string& s){if(s.empty())return {};if(s.size()>32767)throw std::runtime_error("Helper path too long");const auto n=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,s.data(),static_cast<int>(s.size()),nullptr,0);if(!n)throw std::runtime_error("Helper path UTF-8 invalid");std::wstring out(n,L'\0');if(!MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,s.data(),static_cast<int>(s.size()),out.data(),n))throw std::runtime_error("Helper path UTF-8 invalid");return out;}
std::string utf8(const std::wstring& s){if(s.empty())return {};const auto n=WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,s.data(),static_cast<int>(s.size()),nullptr,0,nullptr,nullptr);if(!n)throw std::runtime_error("Helper path Unicode invalid");std::string out(n,'\0');if(!WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,s.data(),static_cast<int>(s.size()),out.data(),n,nullptr,nullptr))throw std::runtime_error("Helper path Unicode invalid");return out;}
std::wstring normalized(std::wstring path){if(path.find(L'\0')!=std::wstring::npos||path.size()>32767)throw std::runtime_error("Invalid helper path");while(!path.empty()&&(path.front()==L' '||path.front()==L'\"'))path.erase(path.begin());while(!path.empty()&&(path.back()==L' '||path.back()==L'\"'))path.pop_back();if(path.empty())return {};DWORD n=ExpandEnvironmentStringsW(path.c_str(),nullptr,0);if(!n||n>32767)throw std::runtime_error("Cannot expand helper path");std::vector<wchar_t> b(n);if(!ExpandEnvironmentStringsW(path.c_str(),b.data(),n))throw std::runtime_error("Cannot expand helper path");path=b.data();if(!std::filesystem::path(path).is_absolute())throw std::runtime_error("Helper path must be absolute");if(_wcsicmp(std::filesystem::path(path).extension().c_str(),L".exe")!=0)path=(std::filesystem::path(path)/L"nvtune.exe").wstring();return path;}
bool exists(const std::wstring& path){const auto attributes=GetFileAttributesW(path.c_str());return attributes!=INVALID_FILE_ATTRIBUTES&&!(attributes&FILE_ATTRIBUTE_DIRECTORY);}
bool config_exists(const std::wstring& p){const auto a=GetFileAttributesW(p.c_str());if(a!=INVALID_FILE_ATTRIBUTES)return true;const auto e=GetLastError();if(e==ERROR_FILE_NOT_FOUND||e==ERROR_PATH_NOT_FOUND)return false;throw std::runtime_error("Cannot inspect timing helper configuration");}
ProfileJson object(const std::wstring& p){auto j=read_profile(p);if(j.contains("nvtune_exe")&&!j["nvtune_exe"].is_null()&&!j["nvtune_exe"].is_string())throw std::runtime_error("Configured helper must be a string");return j;}
void write(const TimingHelperConfigPaths& paths,const std::wstring& path){if(paths.current.empty())throw std::runtime_error("Timing helper config path unavailable");ProfileJson doc=ProfileJson::object();if(config_exists(paths.current))doc=object(paths.current);else if(!paths.legacy.empty()&&config_exists(paths.legacy))doc=object(paths.legacy);const auto parent=std::filesystem::path(paths.current).parent_path();if(parent.empty())throw std::runtime_error("Timing helper config directory unavailable");std::filesystem::create_directories(parent);doc["nvtune_exe"]=utf8(path);write_profile(paths.current,doc);}
}
TimingHelperConfigPaths timing_helper_config_paths(){auto base=env(L"LOCALAPPDATA");if(base.empty())base=env(L"USERPROFILE");if(base.empty())base=env(L"HOMEDRIVE")+env(L"HOMEPATH");if(base.empty()||!std::filesystem::path(base).is_absolute())throw std::runtime_error("Timing helper home unavailable");return {(std::filesystem::path(base)/L"Thermetery"/L"Druta"/L"nvtune.json").wstring(),(std::filesystem::path(base)/L"TitanTune"/L"nvtune.json").wstring()};}
TimingHelperConfigRead read_timing_helper_config(const TimingHelperConfigPaths& paths){TimingHelperConfigRead out;for(int i=0;i<2;++i){const auto& path=i?paths.legacy:paths.current;if(path.empty()||!config_exists(path))continue;try{const auto doc=object(path);out.legacy=i!=0;if(doc.contains("nvtune_exe")&&doc["nvtune_exe"].is_string()){const auto value=normalized(wide(doc["nvtune_exe"].get<std::string>()));if(!value.empty()){if(exists(value))out.registered_path=value;else out.message="Registered nvtune helper is unavailable";}}return out;}catch(const std::exception& e){out.message=e.what();}}return out;}
void set_timing_helper_config(const TimingHelperConfigPaths& paths,const std::wstring& helper){const auto path=normalized(helper);if(path.empty()||!exists(path))throw std::runtime_error("Selected nvtune helper does not exist");write(paths,path);}
void forget_timing_helper_config(const TimingHelperConfigPaths& paths){write(paths,L"");}
}
