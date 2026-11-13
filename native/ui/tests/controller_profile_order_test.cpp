// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "controller_profile_order.h"
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <exception>
#include <set>
using namespace druta::ui;
static unsigned order_checks=0;
#define CHECK(x) do{++order_checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
template<class F>static void rejects(F f){bool failed=false;try{f();}catch(const std::exception&){failed=true;}CHECK(failed);}
static ProfileJson target(){return {{"uuid","GPU-independent"},{"slot","0000:07:00.0"}};}
static ProfileJson daily(){return {{"format","druta-native-daily"},{"version",2},{"target",target()},{"controls",{{"memory",{{"value",128},{"unit","driver-units"},{"scale",16}}},{"boost",{{"value",25},{"unit","%"}}}}},{"advanced",ProfileJson::array({{{"kind","clock_domain"},{"domain",2},{"value",22000},{"unit","kHz"}},{{"kind","nvvdd"},{"domain",0},{"value",-12500},{"unit","uV"}},{{"kind","msvdd"},{"domain",4},{"value",7500},{"unit","uV"}}})}};}
static ProfileJson scoped(){return {{"format","druta-native-scoped"},{"version",1},{"target",target()},{"entries",ProfileJson::array({{{"kind","policy"},{"policy",7},{"binding","policy identity/channel"},{"unit","mA"},{"value",190000}},{{"kind","rail"},{"rail",1},{"field",3},{"binding","rail identity/geometry"},{"unit","uV"},{"value",-25000}},{{"kind","board"},{"binding","board geometry"},{"unit","mW"},{"value",245000}}})}};}
static void partition(const ProfileJson& original,const std::vector<ProfileComponent>& split){const auto description=inspect_profile(original);CHECK(description.valid);std::set<std::string> found;for(const auto& part:split){const auto d=inspect_profile(part.document);CHECK(d.valid&&d.uuid==description.uuid&&d.slot==description.slot&&part.document.at("target")==original.at("target")&&part.document.at("format")==original.at("format")&&part.document.at("version")==original.at("version"));for(const auto& t:d.targets)CHECK(found.insert(t).second);}CHECK(found==std::set<std::string>(description.targets.begin(),description.targets.end()));}
int main(){
    const auto d=daily(),s=scoped();std::vector<ProfileComponent> source{{"Mixed daily",d},{"Mixed scoped",s}};const auto split=split_controller_dependencies(source);CHECK(split.size()==4&&source[0].document==d&&source[1].document==s&&source[0].name=="Mixed daily");
    partition(d,{split[0],split[1]});partition(s,{split[2],split[3]});CHECK(split[0].document.at("controls").empty());CHECK(split[0].document.at("advanced")==ProfileJson::array({d["advanced"][1],d["advanced"][2]}));CHECK(split[1].document.at("controls")==d.at("controls")&&split[1].document.at("advanced")==ProfileJson::array({d["advanced"][0]}));CHECK(split[2].document.at("entries")==ProfileJson::array({s["entries"][1]})&&split[3].document.at("entries")==ProfileJson::array({s["entries"][0],s["entries"][2]}));
    auto voltage=d;voltage["controls"]=ProfileJson::object();voltage["advanced"]=split[0].document["advanced"];auto ordinary=d;ordinary["advanced"]=split[1].document["advanced"];auto rails=s;rails["entries"]=split[2].document["entries"];auto power=s;power["entries"]=split[3].document["entries"];auto v1=ordinary;v1["version"]=1;v1.erase("advanced");
    for(const auto& doc:{voltage,ordinary,rails,power,v1}){const auto unchanged=split_controller_dependencies({{"Original name",doc}});CHECK(unchanged.size()==1&&unchanged[0].name=="Original name"&&unchanged[0].document==doc);}
    auto advanced_only=d;advanced_only["controls"]=ProfileJson::object();const auto two=split_controller_dependencies({{"Only advanced",advanced_only}});CHECK(two.size()==2);partition(advanced_only,two);CHECK(two[1].document["controls"].empty()&&two[1].document["advanced"].size()==1);
    const auto again=split_controller_dependencies(split);CHECK(again.size()==split.size());for(size_t i=0;i<split.size();++i)CHECK(again[i].document==split[i].document&&again[i].name==split[i].name);
    auto bad=d;bad["advanced"][0]["unit"]="MHz";rejects([&]{split_controller_dependencies({{"Bad ordinary units",bad}});});bad=d;bad["controls"]["unknown"]={{"value",1},{"unit","%"}};rejects([&]{split_controller_dependencies({{"Bad control",bad}});});bad=d;bad["advanced"][1]["extra"]=17;rejects([&]{split_controller_dependencies({{"Unknown voltage field",bad}});});bad=d;bad["target"]["driver"]="not target metadata";rejects([&]{split_controller_dependencies({{"Unknown target field",bad}});});bad=s;bad["entries"][0]["unit"]="A";rejects([&]{split_controller_dependencies({{"Bad policy",bad}});});bad=s;bad["entries"][1]["kind"]="opaque_register";rejects([&]{split_controller_dependencies({{"Bad rail kind",bad}});});bad=s;bad["entries"].push_back(bad["entries"][1]);rejects([&]{split_controller_dependencies({{"Duplicate rail",bad}});});rejects([&]{split_controller_dependencies({{"Good first",d},{"Bad second",bad}});});CHECK(source[0].document==d&&source[1].document==s);
    CHECK(split_controller_dependencies({}).empty());std::printf("%u controller dependency partition checks passed\n",order_checks);
}
