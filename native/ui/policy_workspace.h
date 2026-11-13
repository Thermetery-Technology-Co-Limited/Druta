// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "scoped_profile.h"
#include <optional>
namespace druta::ui {
// Annotation document is display metadata only. Unknown JSON is retained.
class PolicyAnnotations {
public:
    explicit PolicyAnnotations(ProfileJson document=ProfileJson::object());
    std::string get(const ScopedTarget&,const ScopedCurrent&) const;
    void set(const ScopedTarget&,const ScopedCurrent&,const std::string&);
    const ProfileJson& document() const {return document_;}
private: ProfileJson document_;
};
struct PolicyMaxRow {ScopedKey key;std::string binding,unit,reason;int64_t before=0,maximum=0;bool included=false;};
struct PolicyMaxPreview {ScopedTarget target;std::vector<PolicyMaxRow> rows;std::optional<ProfileJson> document;std::optional<ScopedPlan> plan;std::string message;};
// Only explicitly selected policy keys are candidates; board and rails are never implicit.
PolicyMaxPreview preview_policy_max(const ScopedSnapshot&,const std::vector<ScopedKey>& selected);
}
