/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include <drutacore/drutacore.h>
#include <array>
#include <memory>
#include <string>
namespace druta::ui {
struct PerfViewApi {
    decltype(&dc_gpu_identity_get) identity=nullptr;
    decltype(&dc_perf_read_group) read_group=nullptr;
};
enum class PerfFreshness { unavailable, current, stale };
struct PerfGroupView {
    std::string name, message;
    PerfFreshness freshness=PerfFreshness::unavailable;
    int32_t error_kind=DC_ERR_NONE;
    uint32_t attempts=0, architecture=0;
    std::shared_ptr<const dc_perf_snapshot> snapshot;
};
struct PerfViewModel {
    std::array<PerfGroupView,DC_PERF_GROUP_COUNT> groups;
    PerfViewModel() { groups[0].name="limits"; groups[1].name="cf_controller"; groups[2].name="cf_policy"; }
};
// The caller owns one service per selected adapter. refresh() is explicit and
// read-only. Failed reads retain same-target last-good data as historical.
class PerfViewService {
public:
    PerfViewService(PerfViewApi api,dc_gpu* gpu,const dc_gpu_identity& target);
    const PerfViewModel& refresh();
    const PerfViewModel& model() const { return model_; }
private:
    PerfViewApi api_; dc_gpu* gpu_; dc_gpu_identity target_{}; PerfViewModel model_;
};
}
