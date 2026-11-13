// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "i2c_route_cache.h"
#include <filesystem>
#include <stdexcept>
namespace druta::ui {
namespace {
bool string(const ProfileJson& value){return value.is_string() && !value.get_ref<const std::string&>().empty()
    && value.get_ref<const std::string&>().find('\0')==std::string::npos;}
I2cRouteHint decode(const ProfileJson& value) {
    if(!value.is_object() || value.size()!=6 || !value.contains("uuid") || !string(value.at("uuid"))
        || !value.contains("family") || !value.at("family").is_number_unsigned() || !value.contains("port")
        || !value.at("port").is_number_unsigned() || !value.contains("addr7") || !value.at("addr7").is_number_unsigned()
        || !value.contains("recipe") || !string(value.at("recipe")) || !value.contains("contract") || !string(value.at("contract")))
        throw std::runtime_error("Invalid I2C route hint schema");
    const auto family=value.at("family").get<uint64_t>(),port=value.at("port").get<uint64_t>(),address=value.at("addr7").get<uint64_t>();
    if(family<1 || family>6 || port>7 || address<8 || address>0x77)throw std::runtime_error("Invalid I2C route hint geometry");
    I2cRouteHint hint{value.at("uuid").get<std::string>(),static_cast<I2cFamily>(family),
        {static_cast<uint8_t>(port),static_cast<uint8_t>(address)},value.at("recipe").get<std::string>(),value.at("contract").get<std::string>()};
    if(!valid_i2c_route_hint(hint))throw std::runtime_error("Stale or unknown I2C recipe contract");return hint;
}
void document(const ProfileJson& value) {
    if(!value.is_object() || value.size()!=2 || !value.contains("schema") || value.at("schema")!="druta-native-i2c-routes-v1"
        || !value.contains("routes") || !value.at("routes").is_object())throw std::runtime_error("Invalid native I2C route cache");
    if(value.at("routes").size()>256)throw std::runtime_error("I2C route cache exceeds route limit");
    for(const auto& entry:value.at("routes").items())if(decode(entry.value()).uuid!=entry.key())throw std::runtime_error("I2C route cache UUID/key mismatch");
}
ProfileJson empty(){return {{"schema","druta-native-i2c-routes-v1"},{"routes",ProfileJson::object()}};}
}
std::string i2c_route_contract(I2cFamily family,const std::string& recipe) {
    if(family==I2cFamily::mp2888) {
        if(recipe=="mp2888a-offset")return "native-mp2888a-offset-fingerprint-v1";
        if(recipe=="mp2888a-pmbus")return "native-mp2888a-pmbus-fingerprint-v1";
        return {};
    }
    if(recipe!="controller")return {};
    switch(family){
    case I2cFamily::nct3933:return "native-nct3933u-id-settings-v1";
    case I2cFamily::ncp4206:return "native-ncp4206-layout-v1";
    case I2cFamily::up9512r:return "native-up9512r-id-layout-v1";
    case I2cFamily::icx:return "native-icx-id-layout-v1";
    case I2cFamily::mp29816:return "native-mp29816-layout-scale-v1";
    default:return {};
    }
}
bool valid_i2c_route_hint(const I2cRouteHint& hint) {
    if(hint.uuid.empty() || hint.uuid.size()>95 || hint.uuid.find('\0')!=std::string::npos || hint.uuid.find_first_not_of(" \t\r\n")==std::string::npos
        || hint.route.port>7 || hint.route.addr7<8 || hint.route.addr7>0x77)return false;
    const auto contract=i2c_route_contract(hint.family,hint.recipe);return !contract.empty() && hint.contract==contract;
}
std::optional<I2cRouteHint> decode_i2c_route_cache(const ProfileJson& value,const std::string& uuid) {
    document(value);const auto& routes=value.at("routes");if(!routes.contains(uuid))return {};return decode(routes.at(uuid));
}
ProfileJson encode_i2c_route_cache(const ProfileJson& value,const I2cRouteHint& hint) {
    if(!valid_i2c_route_hint(hint))throw std::runtime_error("Cannot remember invalid I2C route hint");
    auto result=value.is_null()?empty():value;document(result);
    result["routes"][hint.uuid]={{"uuid",hint.uuid},{"family",static_cast<uint32_t>(hint.family)},{"port",hint.route.port},
        {"addr7",hint.route.addr7},{"recipe",hint.recipe},{"contract",hint.contract}};
    document(result);return result;
}
std::optional<I2cRouteHint> load_i2c_route_cache(const std::wstring& path,const std::string& uuid) {
    if(path.empty() || uuid.empty() || !std::filesystem::exists(std::filesystem::path(path)))return {};
    return decode_i2c_route_cache(read_profile(path),uuid);
}
void remember_i2c_route(const std::wstring& path,const I2cRouteHint& hint) {
    if(path.empty())return;
    const auto value=std::filesystem::exists(std::filesystem::path(path))?read_profile(path):ProfileJson();
    const auto document=encode_i2c_route_cache(value,hint); // Validate before any filesystem mutation.
    const auto parent=std::filesystem::path(path).parent_path();
    if(!parent.empty())std::filesystem::create_directories(parent);
    write_profile(path,document);
}
}
