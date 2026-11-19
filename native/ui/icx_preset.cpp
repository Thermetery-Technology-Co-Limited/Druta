/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "icx_preset.h"
#include <cmath>
#include <cctype>
#include <map>
#include <stdexcept>
namespace druta::ui {
namespace {
using J = ProfileJson;
void require(bool condition, const char* message) { if (!condition) throw std::runtime_error(message); }
void keys(const J& object, std::initializer_list<const char*> names) {
    require(object.is_object() && object.size()==names.size(), "iCX preset contains missing or unexpected keys");
    for (auto key:names) require(object.contains(key), "iCX preset is missing a required key");
}
int integer(const J& value, int low, int high, int step) {
    require(value.is_number_integer(), "iCX preset setting must be an integer");
    // Compare before conversion so out-of-range unsigned integers cannot wrap.
    double n=value.get<double>();
    require(std::isfinite(n) && n>=low && n<=high && std::floor(n)==n && int(n)%step==0, "iCX preset setting outside range or grid");
    return int(n);
}
const J& validate(const J& doc, bool xoc) {
    keys(doc,{"schema","settings","origin"});
    require(doc["schema"].is_string() && doc["schema"]=="druta.icx.preset.v1", "Unsupported iCX preset schema");
    require(doc["origin"].is_string(), "Invalid iCX preset origin");
    auto origin=doc["origin"].get<std::string>(); size_t chars=0; bool nonspace=false;
    for(unsigned char c:origin) { if((c&0xc0)!=0x80) ++chars; if(!std::isspace(c)) nonspace=true; }
    require(nonspace && chars<=512, "Invalid iCX preset origin");
    const auto& s=doc["settings"];
    keys(s,{"nvvdd_mode","nvvdd_request_mv","fbvdd_request_mv","nvvdd_frequency_khz","fbvdd_frequency_khz","nvvdd_ocp_enabled","fbvdd_ocp_enabled","loadline_level"});
    require(s["nvvdd_mode"].is_string(), "Invalid NVVDD mode");
    if(s["nvvdd_mode"]=="auto") require(s["nvvdd_request_mv"].is_null(), "NVVDD Auto requires a null request");
    else { require(s["nvvdd_mode"]=="manual", "Invalid NVVDD mode"); integer(s["nvvdd_request_mv"],750,xoc?1600:1150,5); }
    integer(s["fbvdd_request_mv"],1250,1450,5);
    integer(s["nvvdd_frequency_khz"],300,1000,100); integer(s["fbvdd_frequency_khz"],300,1000,100);
    require(s["nvvdd_ocp_enabled"].is_boolean() && s["fbvdd_ocp_enabled"].is_boolean(), "OCP state must be Boolean");
    integer(s["loadline_level"],0,15,1); return s;
}
}
ProfileJson capture_icx_preset(const ControllerView& view) {
    require(view.readable && !view.stale, "Current iCX readback is unavailable");
    std::map<std::string,const I2cField*> fields;
    for(const auto& field:view.fields) require(fields.emplace(field.key,&field).second, "Duplicate iCX field");
    auto find=[&](const char* key)->const I2cField& { auto it=fields.find(key); require(it!=fields.end() && !it->second->stale, "Complete current iCX readback is required"); return *it->second; };
    auto number=[&](const char* key)->int { const auto& f=find(key); require(f.value.has_value() && std::isfinite(*f.value) && std::floor(*f.value)==*f.value && *f.value>=0 && *f.value<=1600, "Unknown iCX setting"); return int(*f.value); };
    const auto& nv=find("nvvdd");
    bool automatic=!nv.value && nv.detail=="Auto";
    require(automatic || nv.value.has_value(), "NVVDD mode is unknown");
    int ocp_nv=number("ocp_nvvdd"), ocp_fb=number("ocp_fbvdd");
    require((ocp_nv==0||ocp_nv==1) && (ocp_fb==0||ocp_fb==1), "Invalid OCP readback");
    J s={{"nvvdd_mode",automatic?"auto":"manual"},{"nvvdd_request_mv",automatic?J(nullptr):J(number("nvvdd"))},
      {"fbvdd_request_mv",number("fbvdd")},{"nvvdd_frequency_khz",number("frequency_nvvdd")},{"fbvdd_frequency_khz",number("frequency_fbvdd")},
      {"nvvdd_ocp_enabled",bool(ocp_nv)},{"fbvdd_ocp_enabled",bool(ocp_fb)},{"loadline_level",number("loadline")}};
    J doc={{"schema","druta.icx.preset.v1"},{"settings",s},{"origin","Druta controller requested-state readback"}};
    validate(doc,true); return doc;
}
std::vector<ControllerRequest> icx_preset_requests(const ProfileJson& doc,bool xoc) {
    const auto& s=validate(doc,xoc); std::vector<ControllerRequest> out;
    bool automatic=s["nvvdd_mode"]=="auto";
    if(automatic) out.push_back({"nvvdd",0,true});
    for(auto pair : {std::pair<const char*,const char*>{"nvvdd_ocp_enabled","ocp_nvvdd"},{"fbvdd_ocp_enabled","ocp_fbvdd"}})
        if(s[pair.first].get<bool>()) out.push_back({pair.second,1,false});
    out.push_back({"fbvdd",double(s["fbvdd_request_mv"].get<int>()),false});
    out.push_back({"frequency_nvvdd",double(s["nvvdd_frequency_khz"].get<int>()),false});
    out.push_back({"frequency_fbvdd",double(s["fbvdd_frequency_khz"].get<int>()),false});
    out.push_back({"loadline",double(s["loadline_level"].get<int>()),false});
    if(!automatic) out.push_back({"nvvdd",double(s["nvvdd_request_mv"].get<int>()),false});
    for(auto pair : {std::pair<const char*,const char*>{"nvvdd_ocp_enabled","ocp_nvvdd"},{"fbvdd_ocp_enabled","ocp_fbvdd"}})
        if(!s[pair.first].get<bool>()) out.push_back({pair.second,0,false});
    return out;
}
std::string describe_icx_preset(const ProfileJson& doc) {
    const auto& s=validate(doc,true);
    std::string nv=s["nvvdd_mode"]=="auto" ? "Auto" : "manual "+std::to_string(s["nvvdd_request_mv"].get<int>())+" mV";
    return "NVVDD "+nv+"; FBVDD "+std::to_string(s["fbvdd_request_mv"].get<int>())+" mV; switching frequency NVVDD "+
        std::to_string(s["nvvdd_frequency_khz"].get<int>())+" kHz / FBVDD "+std::to_string(s["fbvdd_frequency_khz"].get<int>())+
        " kHz; OCP NVVDD "+(s["nvvdd_ocp_enabled"].get<bool>()?"enabled":"disabled")+" / FBVDD "+
        (s["fbvdd_ocp_enabled"].get<bool>()?"enabled":"disabled")+"; loadline Level "+std::to_string(s["loadline_level"].get<int>())+".";
}
}
