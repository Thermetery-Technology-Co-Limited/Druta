// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "profile_capture_request.h"
#include <stdexcept>
namespace druta::ui {
Command profile_capture_request(ProfileScope scope,const ViewState& view,const SessionSnapshot& snapshot){
 if(!snapshot.data.connected||snapshot.busy)throw std::runtime_error("Connect the adapter and wait for the current operation before capturing");
 Command c;const auto require_generation=[&](uint64_t generation){if(generation!=snapshot.generation)throw std::runtime_error("Open the selected scope editor for the current adapter first");};
 const auto nonempty=[](size_t count){if(!count)throw std::runtime_error("Select the fields to capture in this scope's editor first");};
 switch(scope){
 case ProfileScope::full_tune:c.operation=Command::Operation::save_full_profile;c.annotation_snapshot=std::make_shared<const PolicyAnnotations>(view.policy_annotations);break;
 case ProfileScope::daily:c.operation=Command::Operation::save_profile;break;
 case ProfileScope::curve:c.operation=Command::Operation::save_curve_profile;break;
 case ProfileScope::fan:c.operation=Command::Operation::save_fan_profile;break;
 case ProfileScope::scoped:{require_generation(view.scoped_profile_view.generation);nonempty(view.scoped_profile_view.selection.size());auto r=std::make_shared<ScopedProfileRequest>();r->selection=view.scoped_profile_view.selection;c.scoped_profile=r;c.operation=Command::Operation::save_scoped_profile;break;}
 case ProfileScope::timing:{require_generation(view.timings_view.generation);auto r=std::make_shared<TimingEditorRequest>();c.timing_editor=r;c.operation=Command::Operation::timings_save_profile;break;}
 case ProfileScope::icx:{const auto& d=snapshot.data.i2c;if(!d.selected||d.family!=I2cFamily::icx)throw std::runtime_error("Select the iCX controller in the I2C page first");c.operation=Command::Operation::i2c_save_profile;c.i2c_route=d.selected;c.i2c_family=d.family;c.i2c_selection_revision=d.selection_revision;c.i2c_preset_revision=d.prepared_preset_revision;break;}
 case ProfileScope::voltage_policy:{require_generation(view.voltage_policy_view.generation);auto r=std::make_shared<VoltagePolicyRequest>();for(size_t i=0;i<3;++i)if(view.voltage_policy_view.included[i])r->selection|=1u<<i;nonempty(r->selection);c.voltage_policy=r;c.operation=Command::Operation::voltage_policy_save;break;}
 case ProfileScope::power_integral:{require_generation(view.power_integral_view.generation);auto keys=std::make_shared<std::vector<PowerIntegralKey>>();for(uint32_t i=0;i<32;++i)for(unsigned j=0;j<2;++j)if(view.power_integral_view.included[i][j])keys->push_back({i,j?PowerIntegralField::upper:PowerIntegralField::lower});nonempty(keys->size());c.integral_selection=keys;c.operation=Command::Operation::power_integral_save;break;}
 case ProfileScope::voltage_tables:{require_generation(view.voltage_tables_view.generation);auto keys=std::make_shared<std::vector<VoltageTableKey>>();uint32_t families=0;for(const auto& d:view.voltage_tables_view.drafts)if(d.include){if(d.key.family!=VoltageTableFamily::requests&&d.key.family!=VoltageTableFamily::offsets)throw std::runtime_error("Unknown voltage table family");families|=d.key.family==VoltageTableFamily::requests?1u:2u;keys->push_back(d.key);}nonempty(keys->size());if(families==3)throw std::runtime_error("Save request tables and installed offsets separately, then compose their profiles");c.voltage_families=families;c.voltage_table_selection=keys;c.operation=Command::Operation::voltage_tables_save;break;}
 case ProfileScope::voltage_limits:{require_generation(view.voltage_limits_view.generation);auto keys=std::make_shared<std::vector<VoltageLimitKey>>();for(const auto& d:view.voltage_limits_view.drafts)if(d.include)keys->push_back(d.key);nonempty(keys->size());c.voltage_limit_selection=keys;c.operation=Command::Operation::voltage_limits_save;break;}
 case ProfileScope::power_saving:{require_generation(view.power_saving_view.generation);auto keys=std::make_shared<std::vector<PowerSavingKey>>();for(const auto& d:view.power_saving_view.drafts)if(d.include)keys->push_back(d.key);nonempty(keys->size());c.power_saving_selection=keys;c.operation=Command::Operation::power_saving_save;break;}
 case ProfileScope::clock_gating:{require_generation(view.clock_gating_view.generation);auto keys=std::make_shared<std::vector<ClockGatingFamily>>();for(size_t i=0;i<4;++i)if(view.clock_gating_view.included[i])keys->push_back(static_cast<ClockGatingFamily>(i));nonempty(keys->size());c.clock_gating_selection=keys;c.operation=Command::Operation::clock_gating_save;break;}
 default:throw std::runtime_error("Choose a supported capture scope");
 }return c;
}
}
