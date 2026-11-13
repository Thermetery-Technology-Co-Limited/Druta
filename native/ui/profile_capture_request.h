// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "ui_view.h"
namespace druta::ui {
// Read-only capture intent. Reuses selections from the existing scope editors;
// drafts are never silently applied or serialized as configured requests.
Command profile_capture_request(ProfileScope,const ViewState&,const SessionSnapshot&);
}
