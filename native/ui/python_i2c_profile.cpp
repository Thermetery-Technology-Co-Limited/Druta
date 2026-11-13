// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "python_i2c_profile.h"
#include "core/sha256.h"
#include <set>
#include <stdexcept>
namespace druta::ui {
namespace {
void require(bool b,const char* why){if(!b)throw std::runtime_error(why);}
uint8_t byte(const ProfileJson& v,unsigned high=255){require(v.is_number_integer()&&!v.is_boolean(),"NCT byte must be an integer");if(v.is_number_unsigned()){const auto n=v.get<uint64_t>();require(n<=high,"NCT byte out of range");return static_cast<uint8_t>(n);}const auto n=v.get<int64_t>();require(n>=0&&static_cast<uint64_t>(n)<=high,"NCT byte out of range");return static_cast<uint8_t>(n);}
std::string text(const ProfileJson& v){require(v.is_string(),"NCT identity must be a string");const auto s=v.get<std::string>();require(!s.empty()&&s.size()<=4096&&s.find('\0')==std::string::npos,"Invalid NCT identity");return s;}
void keys(const ProfileJson& j,const std::set<std::string>& expected){require(j.is_object()&&j.size()==expected.size(),"Invalid NCT profile fields");for(const auto& k:expected)require(j.contains(k),"Missing NCT profile field");}
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
}
