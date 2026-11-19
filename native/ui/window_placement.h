// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <windows.h>
#include <algorithm>
#include <cmath>

namespace druta::ui {

inline SIZE initial_client_pixels(SIZE requested, float default_scale) {
    return {requested.cx ? requested.cx : std::lround(1280 * default_scale),
            requested.cy ? requested.cy : std::lround(800 * default_scale)};
}

// The existing HWND supplies the actual frame size on its current monitor.
inline RECT client_window_rect(RECT outer, SIZE current_client, SIZE requested_client) {
    outer.right += requested_client.cx - current_client.cx;
    outer.bottom += requested_client.cy - current_client.cy;
    return outer;
}

inline RECT fit_window_to_work_area(RECT requested, RECT work, bool fit_width = true, bool fit_height = true) {
    const LONG width = requested.right - requested.left, height = requested.bottom - requested.top;
    const LONG w = fit_width ? std::min(width, work.right - work.left) : width;
    const LONG h = fit_height ? std::min(height, work.bottom - work.top) : height;
    const LONG x = std::clamp(requested.left, work.left, std::max(work.left, work.right - w));
    const LONG y = std::clamp(requested.top, work.top, std::max(work.top, work.bottom - h));
    return {x, y, x + w, y + h};
}

}
