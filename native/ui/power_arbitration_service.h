/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include <drutacore/drutacore.h>
#include <memory>
#include <string>
namespace druta::ui {
struct PowerArbitrationApi {
    decltype(&dc_gpu_identity_get) identity=nullptr;
    decltype(&dc_gpu_lock) lock=nullptr;
    decltype(&dc_gpu_unlock) unlock=nullptr;
    decltype(&dc_cl_max_words) max_words=nullptr;
    decltype(&dc_cl_policy_table) policy_table=nullptr;
    decltype(&dc_cl_table_get) table_get=nullptr;
    decltype(&dc_power_arbitration_geometry) geometry=nullptr;
    decltype(&dc_power_arbitration_decode) decode=nullptr;
    decltype(&dc_rm_transport_reset) reset_transport=nullptr; // optional read-only cache reset for retry
};
enum class ArbitrationFreshness { unavailable, current, stale };
struct PowerArbitrationModel {
    ArbitrationFreshness freshness=ArbitrationFreshness::unavailable;
    int32_t error_kind=DC_ERR_NONE;
    uint32_t attempts=0;
    bool status_observed=false,status_present=false;
    std::string message;
    std::shared_ptr<const dc_arbitration_report> report;
};
// One instance per paired GPU handle. Explicit read-only refresh only.
class PowerArbitrationService {
public:
    PowerArbitrationService(PowerArbitrationApi api,dc_gpu* gpu,const dc_gpu_identity& target);
    const PowerArbitrationModel& refresh();
    const PowerArbitrationModel& model() const {return model_;}
private:
    PowerArbitrationApi api_; dc_gpu* gpu_; dc_gpu_identity target_{}; PowerArbitrationModel model_;
};
}
