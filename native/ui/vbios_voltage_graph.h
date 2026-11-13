// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Bounded read-only port of fa6ee83 vbios_fields.py; format research adapted
// there from NVIDIA VBIOS Vix Editor, Copyright (C) 2026 panchovix (GPL-3.0-or-later).
#pragma once
#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>
namespace druta::ui {
constexpr std::size_t vbios_voltage_max_image_bytes=16u*1024u*1024u;
enum class VbiosVoltageRole {rel,alt,ov,vmin,info_vmin,unnamed,possible_byte};
struct VbiosImageRegion {uint32_t start=0,size=0,header=0;uint16_t vendor=0,device=0;uint8_t type=0;bool complete=false,npds=false;};
struct VbiosObjectRegion {uint32_t id=0,offset=0,size=0,hash_offset=0;bool captured=false;};
struct VbiosVoltageTerm {uint32_t index=0;uint8_t kind=0;uint32_t value=0;std::optional<float> literal;std::array<uint8_t,5> raw{};};
struct VbiosVoltageEquation {
 uint32_t index=0,offset=0;uint8_t kind=0,variable=0,output=0;uint16_t next=0xffff;
 float lower=0,upper=0;std::array<float,3> coefficients{};std::array<uint8_t,25> raw{};
 std::vector<VbiosVoltageTerm> variables,terms;
 std::vector<uint32_t> edges,referenced_by,dependencies,affected;
};
struct VbiosVoltageRailRoot {uint32_t rail=0,identity=0,selector_offset=0,equation=0;VbiosVoltageRole role=VbiosVoltageRole::unnamed;};
struct VbiosVoltageClockRoot {uint32_t equation=0,entry=0,kind=0,offset=0;};
struct VbiosVoltageRoleBinding {
 uint32_t rail=0,equation=0;VbiosVoltageRole role=VbiosVoltageRole::rel;
 bool known=false,shared=true,graph_complete=false,encoding_qualified=false;
 // Qualification of the ROM encoding alone is not authorization to write.
 // REL may legitimately have affected REL/ALT users; expose all of them.
 std::optional<int64_t> stored_lower,stored_value;
 std::optional<std::array<double,2>> rel_range;
 std::vector<uint32_t> dependencies,affected;
 std::vector<VbiosVoltageRailRoot> affected_rail_roots;
 std::vector<VbiosVoltageClockRoot> affected_clock_roots;
};
struct VbiosVoltageGraph {
 bool ok=false,graph_complete=false,clock_roots_complete=false;
 std::string message,graph_error,clock_error;
 uint32_t selector_version=0;uint8_t selector_oem=0;
 uint32_t rail_offset=0,vfe_offset=0,equation_base=0;
 std::vector<VbiosImageRegion> images;std::vector<VbiosObjectRegion> objects;
 std::vector<std::array<uint8_t,29>> variable_rows;
 std::vector<VbiosVoltageEquation> equations;
 std::vector<VbiosVoltageRailRoot> rail_roots;
 std::vector<VbiosVoltageClockRoot> clock_roots;
 std::vector<VbiosVoltageRoleBinding> roles;
};
// No hardware, registry, patching, hash repair or inferred board defaults.
// Malformed ROM/table ownership returns !ok; an incomplete understood sharing
// graph retains inspection with encoding_qualified=false and explicit errors.
VbiosVoltageGraph parse_vbios_voltage_graph(const std::vector<uint8_t>& image);
}
