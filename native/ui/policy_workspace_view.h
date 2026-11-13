// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "policy_workspace.h"
#include <array>
namespace druta::ui {
struct PolicyWorkspaceViewState {bool open=false;uint64_t generation=0;std::vector<ScopedKey> selected;std::optional<PolicyMaxPreview> preview;std::array<char,257> label{};std::string status;};
struct PolicyWorkspaceOutput {bool refresh=false,save_annotations=false;std::optional<ProfileJson> apply;};
PolicyWorkspaceOutput draw_policy_workspace(PolicyWorkspaceViewState&,PolicyAnnotations&,const ScopedSnapshot*,uint64_t generation,bool busy,bool connected,bool unlocked);
}
