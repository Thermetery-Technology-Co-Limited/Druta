// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "i2c_route_cache.h"
#include <cstdio>
#include <filesystem>
#include <windows.h>
using namespace druta::ui;
namespace {
int failures=0;
void check(bool value,const char* label){if(!value){++failures;std::printf("FAIL: %s\n",label);}}
template<class F>bool rejects(F function){try{function();return false;}catch(const std::exception&){return true;}}
I2cRouteHint hint(std::string uuid="GPU-route-alternate") {
    I2cRouteHint h;h.uuid=std::move(uuid);h.family=I2cFamily::mp2888;h.route={5,0x43};h.recipe="mp2888a-pmbus";
    h.contract=i2c_route_contract(h.family,h.recipe);return h;
}
}
int main(){
    const auto a=hint();auto document=encode_i2c_route_cache({},a);
    const auto loaded=decode_i2c_route_cache(document,a.uuid);
    check(loaded && loaded->route.port==5 && loaded->recipe==a.recipe,"UUID bound route and exact recipe roundtrip");
    check(!decode_i2c_route_cache(document,"GPU-other"),"different UUID never falls back to another adapter");
    auto b=hint("GPU-second");b.route={1,0x2d};b.family=I2cFamily::icx;b.recipe="controller";b.contract=i2c_route_contract(b.family,b.recipe);
    document=encode_i2c_route_cache(document,b);check(document["routes"].size()==2 && decode_i2c_route_cache(document,a.uuid).has_value(),"positive update preserves other GPU hint");
    auto bad=document;bad["routes"][a.uuid]["gate"]=true;
    check(rejects([&]{decode_i2c_route_cache(bad,a.uuid);}),"gate/output/verification replay fields rejected");
    bad=document;bad["routes"][a.uuid]["contract"]="old-recipe-v0";
    check(rejects([&]{decode_i2c_route_cache(bad,a.uuid);}),"stale compiled recipe contract is a hint failure");
    bad=document;bad["routes"][a.uuid]["uuid"]="GPU-mismatch";
    check(rejects([&]{decode_i2c_route_cache(bad,a.uuid);}),"key identity mismatch rejected");
    auto invalid=a;invalid.route.addr7=0;check(!valid_i2c_route_hint(invalid),"invalid bus address refused");
    invalid=a;invalid.recipe="unknown-control";check(!valid_i2c_route_hint(invalid),"unknown recipe refused");
    invalid=a;invalid.uuid="  ";check(!valid_i2c_route_hint(invalid),"blank UUID refused");
    wchar_t directory[MAX_PATH]{};GetTempPathW(MAX_PATH,directory);
    const auto path=(std::filesystem::path(directory)/(L"druta-i2c-route-test-"+std::to_wstring(GetCurrentProcessId())+L".json")).wstring();
    std::filesystem::remove(path);
    check(!load_i2c_route_cache(path,a.uuid),"missing cache is no hint");
    remember_i2c_route(path,a);remember_i2c_route(path,b);
    check(load_i2c_route_cache(path,a.uuid).has_value() && load_i2c_route_cache(path,b.uuid).has_value(),"atomic profile_io file updates preserve both UUIDs");
    write_profile(path,bad);const auto malformed=read_profile(path);
    check(rejects([&]{remember_i2c_route(path,a);}) && read_profile(path)==malformed,"malformed existing file remains unchanged");
    std::filesystem::remove(path);
    const auto parent=std::filesystem::path(directory)/(L"druta-i2c-cache-nested-"+std::to_wstring(GetCurrentProcessId()));
    const auto nested=(parent/L"fresh-state"/L"i2c-routes-native.json").wstring();
    check(!std::filesystem::exists(parent),"fresh nested cache fixture starts without parent directories");
    remember_i2c_route(nested,a);check(load_i2c_route_cache(nested,a.uuid).has_value(),"first route save creates fresh nested state directories before atomic write");
    std::filesystem::remove(nested);std::filesystem::remove(parent/L"fresh-state");std::filesystem::remove(parent);
    std::printf("I2C route cache: %d failures\n",failures);return failures?1:0;
}
