/* Standalone test runner. Build with the x64 MSVC toolchain, without a CRT.
   Usage is documented in Verify-Druta.cmd. */
#define _WIN32_WINNT 0x0502
#define UNICODE
#define _UNICODE
#include <windows.h>
#include <wincrypt.h>
#define COUNT(a) (sizeof(a)/sizeof((a)[0]))

static OSVERSIONINFOEXW os;
static WCHAR target[64], action[64], outdir[2048], module_path[2048];
static WCHAR command[4096], app_path[2048];
static STARTUPINFOW startup;
static PROCESS_INFORMATION child;
static BYTE hash_buffer[8192];
static void say(const char *s) { DWORD count; WriteFile(GetStdHandle(STD_OUTPUT_HANDLE),s,(DWORD)lstrlenA(s),&count,0); }
static void line_u(const char *name,DWORD value) {
    char buf[180]; wsprintfA(buf,"%s=%lu\r\n",name,value); say(buf);
}
static void line_error(const char *name) { line_u(name,GetLastError()); }
static BOOL absfile(const WCHAR *name,WCHAR *path,DWORD size) {
    DWORD n=GetFullPathNameW(name,size,path,0); return n && n<size;
}
static const WCHAR *const images[]={L"druta-ui.exe",L"drutacore.dll",L"druta-probe.exe",L"druta-ui-timing-cuda.exe"};
static const char *const names[]={"druta-ui.exe","drutacore.dll","druta-probe.exe","druta-ui-timing-cuda.exe"};
static BOOL os_match(void) {
    typedef LONG (WINAPI *version_fn)(OSVERSIONINFOEXW *);
    version_fn get=(version_fn)GetProcAddress(GetModuleHandleW(L"ntdll.dll"),"RtlGetVersion");
    os.dwOSVersionInfoSize=sizeof(os);
    if(!get || get(&os)!=0) return FALSE;
    line_u("nt_major",os.dwMajorVersion); line_u("nt_minor",os.dwMinorVersion);
    line_u("build",os.dwBuildNumber); line_u("service_pack",os.wServicePackMajor);
    line_u("product_type",os.wProductType); say("runner_arch=AMD64\r\n");
    if(lstrcmpiW(target,L"host")==0){say("target=host control only\r\n"); return TRUE;}
    if(os.wProductType!=VER_NT_WORKSTATION) return FALSE;
    if(!lstrcmpiW(target,L"win7"))return os.dwMajorVersion==6 && os.dwMinorVersion==1 && os.wServicePackMajor==1;
    if(!lstrcmpiW(target,L"vista"))return os.dwMajorVersion==6 && os.dwMinorVersion==0 && os.wServicePackMajor==2;
    if(!lstrcmpiW(target,L"xp"))return os.dwMajorVersion==5 && os.dwMinorVersion==2 && os.wServicePackMajor==2;
    return FALSE;
}
static BOOL hash_files(void) {
    HCRYPTPROV provider=0;
    if(!CryptAcquireContextW(&provider,0,0,PROV_RSA_AES,CRYPT_VERIFYCONTEXT)){
        line_error("sha256_provider_error");return FALSE;
    }
    BOOL ok=TRUE;
    for(unsigned i=0;i<COUNT(images);++i) {
        HCRYPTHASH hash=0; DWORD n=0; BYTE digest[32]; DWORD bytes=sizeof(digest);
        HANDLE file=CreateFileW(images[i],GENERIC_READ,FILE_SHARE_READ,0,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,0);
        BOOL good=file!=INVALID_HANDLE_VALUE && CryptCreateHash(provider,CALG_SHA_256,0,0,&hash);
        while(good) {
            good=ReadFile(file,hash_buffer,sizeof(hash_buffer),&n,0);
            if(!good || !n)break;
            good=CryptHashData(hash,hash_buffer,n,0);
        }
        if(good)good=CryptGetHashParam(hash,HP_HASHVAL,digest,&bytes,0) && bytes==32;
        if(good){
            const char hex[]="0123456789abcdef"; char value[65];
            for(DWORD j=0;j<32;++j){value[2*j]=hex[digest[j]>>4];value[2*j+1]=hex[digest[j]&15];}
            value[64]='\0';say(names[i]);say(" SHA256 ");say(value);say("\r\n");
        }else{say(names[i]);say(" ");line_error("sha256_error");ok=FALSE;}
        if(hash)CryptDestroyHash(hash);
        if(file!=INVALID_HANDLE_VALUE)CloseHandle(file);
    }
    CryptReleaseContext(provider,0);return ok;
}
static BOOL core_abi(void) {
    typedef DWORD (__cdecl *abi_fn)(void);
    if(!absfile(L"drutacore.dll",module_path,COUNT(module_path)))return FALSE;
    HMODULE core=LoadLibraryExW(module_path,0,LOAD_WITH_ALTERED_SEARCH_PATH);
    if(!core){line_error("core_load_error");return FALSE;}
    abi_fn abi=(abi_fn)GetProcAddress(core,"dc_abi_version");
    BOOL good=FALSE;
    if(abi){DWORD version=abi();line_u("core_abi",version);good=version==2;}
    else line_error("core_export_error");
    FreeLibrary(core);return good;
}
static BOOL child_test(unsigned image, DWORD expected,DWORD timeout_ms) {
    if(!absfile(images[image],app_path,COUNT(app_path)))return FALSE;
    if((size_t)lstrlenW(app_path)+lstrlenW(outdir)+40>=COUNT(command))return FALSE;
    lstrcpyW(command,L"\"");lstrcatW(command,app_path);lstrcatW(command,L"\"");
    if(image==0){lstrcatW(command,L" --smoke-test \"");lstrcatW(command,outdir);lstrcatW(command,L"\\gui\"");}
    startup.cb=sizeof(startup);
    startup.dwFlags=STARTF_USESTDHANDLES;
    startup.hStdInput=GetStdHandle(STD_INPUT_HANDLE);
    startup.hStdOutput=GetStdHandle(STD_OUTPUT_HANDLE);startup.hStdError=GetStdHandle(STD_ERROR_HANDLE);
    if(!CreateProcessW(app_path,command,0,0,TRUE,0,0,0,&startup,&child)){line_error("create_process_error");return FALSE;}
    DWORD wait=WaitForSingleObject(child.hProcess,timeout_ms),code=0xffffffff;
    BOOL success=wait==WAIT_OBJECT_0 && GetExitCodeProcess(child.hProcess,&code) && code==expected;
    if(wait!=WAIT_OBJECT_0){
        say("child did not exit within the test deadline\r\n");
        TerminateProcess(child.hProcess,124);WaitForSingleObject(child.hProcess,5000);
    }
    line_u("child_exit",code);CloseHandle(child.hThread);CloseHandle(child.hProcess);return success;
}
void entry(void) {
    SetErrorMode(SEM_FAILCRITICALERRORS|SEM_NOGPFAULTERRORBOX|SEM_NOOPENFILEERRORBOX);
    DWORD na=GetEnvironmentVariableW(L"DRUTA_VERIFY_ACTION",action,COUNT(action));
    DWORD nt=GetEnvironmentVariableW(L"DRUTA_VERIFY_OS",target,COUNT(target));
    DWORD nd=GetEnvironmentVariableW(L"DRUTA_VERIFY_OUTPUT",outdir,COUNT(outdir));
    if(!na||na>=COUNT(action)||!nt||nt>=COUNT(target)||!nd||nd>=COUNT(outdir))ExitProcess(2);
    BOOL ok=FALSE;
    if(!lstrcmpiW(action,L"os"))ok=os_match();
    else if(!lstrcmpiW(action,L"hash"))ok=hash_files();
    else if(!lstrcmpiW(action,L"abi"))ok=core_abi();
    else if(!lstrcmpiW(action,L"gui"))ok=child_test(0,0,120000);
    else if(!lstrcmpiW(action,L"probe"))ok=child_test(2,0,45000);
    else if(!lstrcmpiW(action,L"worker"))ok=child_test(3,2,15000);
    ExitProcess(ok?0:1);
}
