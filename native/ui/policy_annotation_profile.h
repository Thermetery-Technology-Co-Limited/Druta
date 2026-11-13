// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "policy_workspace.h"
#include <memory>
#include <map>
namespace druta::ui {
enum class PolicyAnnotationPhase { apply,undo };
struct PolicyAnnotationRow {ScopedCurrent identity;std::string before,desired;uint64_t before_revision=0;};
struct PolicyAnnotationPlan {uint64_t transaction=0;ScopedTarget target;std::vector<PolicyAnnotationRow> rows;size_t skipped=0;};
struct PolicyAnnotationEffect {std::shared_ptr<const PolicyAnnotationPlan> plan;PolicyAnnotationPhase phase=PolicyAnnotationPhase::apply;std::vector<ScopedCurrent> rows;size_t skipped=0;};
struct PolicyAnnotationCommittedRow {PolicyAnnotationRow row;uint64_t applied_revision=0;};
// Worker Undo and queued effects own plans. The host bookkeeping must not
// prolong transactions after both have discarded them.
struct PolicyAnnotationCommitReceipt {std::weak_ptr<const PolicyAnnotationPlan> plan;std::vector<PolicyAnnotationCommittedRow> rows;bool accepted=false,undone=false;std::string message;};
struct PolicyAnnotationCommits {std::map<uint64_t,PolicyAnnotationCommitReceipt> transactions;};
struct PolicyAnnotationCommitResult {bool ok=false,changed=false,complete=false;size_t applied=0,skipped=0;std::string message;};
// All functions are pure with respect to hardware/files. Preparing/qualifying
// never changes the UI store; only the UI thread calls commit explicitly.
std::shared_ptr<const PolicyAnnotationPlan> prepare_policy_annotation_profile(const ProfileJson&,const ScopedSnapshot&,const PolicyAnnotations&);
std::shared_ptr<const PolicyAnnotationEffect> qualify_policy_annotation_effect(std::shared_ptr<const PolicyAnnotationPlan>,PolicyAnnotationPhase,const ScopedSnapshot&);
PolicyAnnotationCommitResult commit_policy_annotation_effect(const PolicyAnnotationEffect&,PolicyAnnotations&,PolicyAnnotationCommits&);
}
