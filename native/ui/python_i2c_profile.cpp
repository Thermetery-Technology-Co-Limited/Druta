// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "python_i2c_profile.h"
#include "core/sha256.h"
#include <set>
#include <cstdio>
#include <stdexcept>
namespace druta::ui {
namespace {
void require(bool b,const char* why){if(!b)throw std::runtime_error(why);}
uint8_t byte(const ProfileJson& v,unsigned high=255){require(v.is_number_integer()&&!v.is_boolean(),"I2C byte must be an integer");if(v.is_number_unsigned()){const auto n=v.get<uint64_t>();require(n<=high,"I2C byte out of range");return static_cast<uint8_t>(n);}const auto n=v.get<int64_t>();require(n>=0&&static_cast<uint64_t>(n)<=high,"I2C byte out of range");return static_cast<uint8_t>(n);}
std::string text(const ProfileJson& v){require(v.is_string(),"I2C identity must be a string");const auto s=v.get<std::string>();require(!s.empty()&&s.size()<=4096&&s.find('\0')==std::string::npos,"Invalid I2C identity");return s;}
void keys(const ProfileJson& j,const std::set<std::string>& expected){require(j.is_object()&&j.size()==expected.size(),"Invalid I2C profile fields");for(const auto& k:expected)require(j.contains(k),"Missing I2C profile field");}
}
PythonNct3933Profile parse_python_nct3933_profile(const ProfileJson& j){
 require(j.is_object()&&j.value("format",ProfileJson())=="i2c.current_dac_control","This I2C saved-control format has no qualified native profile adapter yet");
 keys(j,{"profile","sha256","port","addr7","rail","format","current_dac_control","display_name"});
 PythonNct3933Profile result;result.profile=text(j["profile"]);result.rail=text(j["rail"]);result.recipe_sha256=text(j["sha256"]);
 require(text(j["format"])=="i2c.current_dac_control","Unsupported I2C controller profile format");
 require(result.profile=="NCT3933U - three current offsets"&&result.rail=="OUT1/OUT2/OUT3","Unsupported NCT released recipe identity");(void)text(j["display_name"]);
 result.route.port=byte(j["port"],7);result.route.addr7=byte(j["addr7"],0x15);require(result.route.addr7>=0x10,"Unsupported NCT address");
 const auto recipe=std::string("{\"addr7\": ")+std::to_string(result.route.addr7)+", \"contract\": 1, \"controller\": \"NCT3933U\", \"port\": "+std::to_string(result.route.port)+", \"units\": \"nominal_microamps\"}";
 require(result.recipe_sha256==druta::sha256_hex(reinterpret_cast<const uint8_t*>(recipe.data()),recipe.size()),"NCT released recipe hash mismatch");
 const auto& control=j["current_dac_control"];keys(control,{"outputs","configuration"});require(control["outputs"].is_array()&&control["outputs"].size()==3,"NCT requires three outputs");
 for(size_t i=0;i<3;++i)result.outputs[i]=byte(control["outputs"][i]);result.configuration=byte(control["configuration"]);return result;
}
namespace {
struct CompoundIdentity {I2cRoute route{};std::string profile,rail,hash;};
CompoundIdentity compound(const ProfileJson& j,bool up){
 if(up)keys(j,{"profile","sha256","port","addr7","rail","format","control","display_name"});else keys(j,{"profile","sha256","port","addr7","rail","control","display_name"});
 CompoundIdentity c;c.route.port=byte(j["port"],7);c.route.addr7=byte(j["addr7"],0x77);require(c.route.addr7>=8,"Unsupported controller address");
 char label[120];std::snprintf(label,sizeof(label),up?"uP9512R offsets (port %u, 0x%02X; physical rail unassigned)":"NCP4206 output (port %u, 0x%02X; physical rail unassigned)",unsigned(c.route.port),unsigned(c.route.addr7));
 c.profile=text(j["profile"]);c.rail=text(j["rail"]);c.hash=text(j["sha256"]);(void)text(j["display_name"]);require(c.profile==label&&c.rail=="Controller output","Unsupported released controller recipe identity");
 if(up)require(text(j["format"])=="i2c.multi_state_offset","Unsupported uP controller format");
 const auto port=std::to_string(c.route.port),addr=std::to_string(c.route.addr7);
 const auto recipe=up?"{\"addr7\": "+addr+", \"identity\": [0, 43], \"kind\": \"up9512r-offset-v1\", \"port\": "+port+"}":"{\"addr7\": "+addr+", \"identity\": null, \"kind\": \"ncp4206-absolute-v1\", \"min_mv\": 600, \"normal_max\": 1281, \"port\": "+port+", \"vid_max\": 1600, \"xoc_max\": 2000}";
 require(c.hash==druta::sha256_hex(reinterpret_cast<const uint8_t*>(recipe.data()),recipe.size()),"Released controller recipe hash mismatch");return c;
}
}
PythonUp9512rProfile parse_python_up9512r_profile(const ProfileJson& j){const auto c=compound(j,true);const auto& state=j["control"];keys(state,{"kind","offsets_mv","enabled"});require(text(state["kind"])=="up9512r-offset-v1"&&state["enabled"].is_boolean()&&state["offsets_mv"].is_array()&&state["offsets_mv"].size()==5,"Invalid uP9512R compound state");PythonUp9512rProfile out;out.route=c.route;out.profile=c.profile;out.rail=c.rail;out.recipe_sha256=c.hash;out.enabled=state["enabled"].get<bool>();for(size_t i=0;i<5;++i){out.offsets_mv[i]=byte(state["offsets_mv"][i],150);require(out.offsets_mv[i]%10==0,"uP9512R offset must be in 10 mV steps");}return out;}
PythonNcp4206Profile parse_python_ncp4206_profile(const ProfileJson& j){const auto c=compound(j,false);const auto& state=j["control"];keys(state,{"kind","enabled","command"});require(text(state["kind"])=="absolute_vid"&&state["enabled"].is_boolean(),"Invalid NCP4206 absolute state");PythonNcp4206Profile out;out.route=c.route;out.profile=c.profile;out.rail=c.rail;out.recipe_sha256=c.hash;out.enabled=state["enabled"].get<bool>();out.command=byte(state["command"]);require(!out.enabled||(out.command>=2&&out.command<=198),"Active NCP4206 command is not a VID");return out;
}
}
