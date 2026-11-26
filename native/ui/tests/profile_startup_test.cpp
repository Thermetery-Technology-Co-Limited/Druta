// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "profile_startup.h"
#include "profile_catalog.h"
#include "policy_workspace.h"
#include "refresh_policy.h"
#include "session.h"
#include <windows.h>
#include <taskschd.h>
#include <cstdio>
#include <cstdlib>
#include <atomic>

using namespace druta::ui;
#define CHECK(x) do { if (!(x)) { std::fprintf(stderr,"line %d: %s\n",__LINE__,#x); std::abort(); } } while(false)

static StartupProfile daily() {
    return {"library/Daily","Everyday & Quiet","gpu-a","slot-a", {
        {"format","druta-native-daily"},{"version",1},{"target",{{"uuid","gpu-a"},{"slot","slot-a"}}},
        {"controls",{{"core",{{"value",15},{"unit","MHz"}}}}}
    }};
}

struct FakeBackend : IBackend {
    std::atomic<unsigned> reads{0}, actions{0};
    bool accept = true;
    HANDLE entered = CreateEventW(nullptr,TRUE,FALSE,nullptr);
    HANDLE proceed = CreateEventW(nullptr,TRUE,FALSE,nullptr);
    ~FakeBackend() { CloseHandle(entered); CloseHandle(proceed); }
    std::vector<Device> enumerate() override { return {{"slot-a","GPU","gpu-a"}}; }
    bool open(const std::string& slot, std::string&) override { return slot == "slot-a"; }
    void close() override {}
    LiveData read(bool) override {
        ++reads;
        LiveData data;
        data.connected = data.telemetry_valid = true;
        data.identity.nvapi_ok = 1;
        strcpy_s(data.identity.uuid,"gpu-a");
        strcpy_s(data.identity.slot,"slot-a");
        return data;
    }
    ActionResult execute(const Command& c) override {
        CHECK(c.operation == Command::Operation::apply_catalog_profile);
        CHECK(c.catalog_profile && *c.catalog_profile == daily().document && c.annotation_snapshot);
        ++actions;
        SetEvent(entered);
        CHECK(WaitForSingleObject(proceed,3000) == WAIT_OBJECT_0);
        ActionResult result;
        result.ok = accept;
        result.message = accept ? "Verified requested fields" : "Fresh preflight refused the request";
        return result;
    }
};

template<class F> static void until(F f) {
    const auto started = GetTickCount64();
    while (!f() && GetTickCount64() - started < 3000) Sleep(1);
    CHECK(f());
}
static void startup_session(bool accepted, bool restore_early, bool matching) {
    auto backend = std::make_unique<FakeBackend>();
    auto raw = backend.get();
    raw->accept = accepted;
    Session session(std::move(backend),[]{});
    auto p = daily();
    if (!matching) p.uuid = "other";
    session.start(p.slot,p.uuid);
    StartupProfileLaunch launch;
    launch.start(session,p,true);
    PolicyAnnotations notes;
    until([&]{ return !session.snapshot()->opening; });
    auto result = launch.tick(session,true,notes,0,true);
    if (matching) {
        CHECK(!result);
        CHECK(WaitForSingleObject(raw->entered,3000) == WAIT_OBJECT_0);
        CHECK(!launch.tick(session,true,notes,0,true) && !session.snapshot()->paused);
        if (restore_early) StartupProfileLaunch::resume(session);
        SetEvent(raw->proceed);
        until([&]{return !session.snapshot()->busy;});
        result = launch.tick(session,!restore_early,notes,0,true);
    }
    CHECK(result && result->ok == (accepted && matching));
    CHECK(!launch.pending() && !launch.tick(session,true,notes,0,true));
    CHECK(raw->actions == (matching ? 1u : 0u));
    CHECK(session.snapshot()->paused == !restore_early);
    if (!restore_early && matching) {
        const auto reads = raw->reads.load();
        session.request_poll();
        Sleep(30);
        CHECK(raw->reads == reads);
        StartupProfileLaunch::resume(session);
        until([&]{return raw->reads > reads;});
        CHECK(!session.snapshot()->paused);
        CHECK(session.reconnect());
        until([&]{return !session.snapshot()->opening;});
        CHECK(!launch.tick(session,false,notes,0,true) && raw->actions == 1);
    }
}

// Parse the actual production task, then read back the approved profile from
// the in-memory definition. No call registers a task or launches its action.
static void validate_task_xml() {
    CHECK(SUCCEEDED(CoInitializeEx(nullptr,COINIT_APARTMENTTHREADED)));
    ITaskService* scheduler = nullptr;
    CHECK(SUCCEEDED(CoCreateInstance(CLSID_TaskScheduler,nullptr,CLSCTX_INPROC_SERVER,IID_ITaskService,
                                      reinterpret_cast<void**>(&scheduler))));
    VARIANT empty{};
    CHECK(SUCCEEDED(scheduler->Connect(empty,empty,empty,empty)));
    ITaskDefinition* definition = nullptr;
    CHECK(SUCCEEDED(scheduler->NewTask(0,&definition)));
    const auto xml = startup_task_xml(daily(),L"C:\\Druta & Tests\\Druta.exe",L"C:\\Druta & Tests\\drutacore.dll",L"S-1-5-21-123-456-789-1001");
    BSTR text = SysAllocString(xml.c_str());
    const HRESULT parsed = definition->put_XmlText(text);
    if (FAILED(parsed)) std::fprintf(stderr,"Task XML HRESULT %08lX\n",static_cast<unsigned long>(parsed));
    CHECK(SUCCEEDED(parsed));
    SysFreeString(text);
    IRegistrationInfo* info = nullptr;
    CHECK(SUCCEEDED(definition->get_RegistrationInfo(&info)));
    BSTR doc = nullptr;
    CHECK(SUCCEEDED(info->get_Documentation(&doc)) && doc);
    std::wstring wide(doc);
    std::string bytes;
    for (wchar_t c : wide) bytes.push_back(static_cast<char>(c));
    const auto profile = ProfileJson::parse(bytes);
    CHECK(profile["profile"] == daily().document && profile["name"] == daily().name);
    SysFreeString(doc);
    info->Release();
    IPrincipal* principal = nullptr;
    CHECK(SUCCEEDED(definition->get_Principal(&principal)));
    TASK_RUNLEVEL_TYPE level;
    CHECK(SUCCEEDED(principal->get_RunLevel(&level)) && level == TASK_RUNLEVEL_LUA);
    principal->Release();
    definition->Release();
    scheduler->Release();
    CoUninitialize();
}
int main() {
    auto description = inspect_profile(daily().document);
    CHECK(startup_profile_reason(description,"gpu-a","slot-a").empty());
    CHECK(!startup_profile_reason(description,"gpu-b","slot-a").empty());
    for (auto target : {"clock-gating:3","i2c:controller","rail:0:0"}) {
        auto restricted = description;
        restricted.targets.push_back(target);
        CHECK(!startup_profile_reason(restricted,"gpu-a","slot-a").empty());
    }
    ProfileJson journal = ProfileJson::object();
    CHECK(!claim_startup_boot(journal,{"one",false}).empty());
    CHECK(!claim_startup_boot(journal,{"one",true}).empty());
    CHECK(claim_startup_boot(journal,{"two",true}).empty());
    CHECK(!claim_startup_boot(journal,{"two",true}).empty() && journal["in_progress"] == true);
    CHECK(!claim_startup_boot(journal,{"three",true}).empty() && journal["in_progress"] == false);
    CHECK(claim_startup_boot(journal,{"four",true}).empty());
    startup_session(true,false,true);
    startup_session(false,false,true);
    startup_session(true,true,true);
    startup_session(true,false,false);
    validate_task_xml();
    std::puts("Startup profile session, boot journal, validation and task schema passed.");
}
