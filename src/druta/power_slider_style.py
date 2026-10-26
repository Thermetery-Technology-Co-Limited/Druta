# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""A clipped tint for the extended part of an existing native power slider.

The slider, its callbacks, and its native mouse/keyboard interaction stay intact.
DearPyGui's disabled group supplies a non-interactive drawing surface in the
same table cell. Unlike a viewport drawing, its drawlist inherits the enclosing
window/table clip. Call ``refresh`` before rendering and after value changes.
"""

from __future__ import annotations

import math

_UNCHANGED = object()


def _bounds(value):
    if value is None:
        return None
    try:
        lo, hi = (float(x) for x in value)
    except (TypeError, ValueError, OverflowError):
        return None
    return (lo, hi) if math.isfinite(lo) and math.isfinite(hi) and lo <= hi else None


def extended_segments(bounds, original_bounds, width):
    """Return pixel intervals outside the original range, clipped to the track.

    The original range may extend beyond the displayed slider range. Unknown or
    invalid ranges yield no tint; an unknown BIOS range must not imply an
    extension. Boundaries themselves belong to the normal range.
    """
    active, original = _bounds(bounds), _bounds(original_bounds)
    if active is None or original is None or not math.isfinite(width) or width <= 0:
        return ()
    lo, hi = active
    if hi <= lo:
        return ()

    def x(value):
        return width * min(1.0, max(0.0, (value - lo) / (hi - lo)))

    result = []
    left, right = x(original[0]), x(original[1])
    if left > 0:
        result.append((0.0, left))
    if right < width:
        result.append((right, float(width)))
    return tuple(result)


def outside_original(value, original_bounds):
    """Whether a watt input is outside the inclusive BIOS range on its mW grid.

    DearPyGui stores float inputs as C floats. Quantizing exactly as the power
    request does keeps, for example, a stored 450.001007 W endpoint at 450001 mW.
    """
    original = _bounds(original_bounds)
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return bool(
        original
        and math.isfinite(value)
        and not round(original[0] * 1000) <= round(value * 1000) <= round(original[1] * 1000)
    )


class PowerSliderStyle:
    """Decorate an existing slider and its adjacent numeric input.

    ``original_bounds`` is in watts, as is the slider; ``None`` means that
    the original range is unavailable. The slider must already be inside its
    own group (as in ``Druta.slider_row``). This object owns only a disabled
    overlay group and an amber text-only theme. Normal text inherits the input's
    original theme (``normal_text`` is retained as a compatibility keyword).
    Close it before rebuilding the UI,
    then create a new instance for the new slider/input items.
    """

    def __init__(
        self,
        slider,
        input_item,
        *,
        original_bounds=None,
        amber=(255, 203, 71),
        normal_text=(230, 232, 236),
    ):
        import dearpygui.dearpygui as dpg

        self._dpg = dpg
        self.slider = slider
        self.input_item = input_item
        self.original_bounds = _bounds(original_bounds)
        self._amber = tuple(amber[:3])
        self._previous_theme = dpg.get_item_theme(input_item)
        self._closed = False
        self._last_geometry = None
        self._last_theme = None
        parent = dpg.get_item_parent(slider)
        with dpg.group(parent=parent, enabled=False, show=False) as self._overlay:
            with dpg.drawlist(width=1, height=1) as self._drawlist:
                self._rectangles = [
                    dpg.draw_rectangle((0, 0), (1, 1), color=(0, 0, 0, 0), show=False) for _ in range(2)
                ]
        with dpg.theme() as theme:
            for enabled in (True, False):
                with dpg.theme_component(dpg.mvAll, enabled_state=enabled):
                    text = (*amber[:3], 255 if enabled else 150)
                    dpg.add_theme_color(dpg.mvThemeCol_Text, text)
                    if not enabled:
                        dpg.add_theme_color(dpg.mvThemeCol_TextDisabled, text)
        self._themes = [self._previous_theme, theme]

    def _enabled(self):
        item = self.slider
        while item:
            if not self._dpg.get_item_configuration(item).get("enabled", True):
                return False
            item = self._dpg.get_item_parent(item)
        return True

    def refresh(self, *, original_bounds=_UNCHANGED):
        """Refresh geometry and text; return False after either widget is deleted.

        This does not write widget values, change bounds, replace callbacks, or
        invoke a device operation. Passing ``original_bounds=None`` explicitly
        removes the tint; omitting it retains the current original range.
        """
        dpg = self._dpg
        if self._closed:
            return False
        if original_bounds is not _UNCHANGED:
            self.original_bounds = _bounds(original_bounds)
        if not all(dpg.does_item_exist(item) for item in (self.slider, self.input_item, self._overlay)):
            self.close()
            return False

        theme = self._themes[int(outside_original(dpg.get_value(self.input_item), self.original_bounds))]
        if theme != self._last_theme or dpg.get_item_theme(self.input_item) != theme:
            dpg.bind_item_theme(self.input_item, theme)
            self._last_theme = theme

        config = dpg.get_item_configuration(self.slider)
        width, height = dpg.get_item_rect_size(self.slider)
        segments = extended_segments((config["min_value"], config["max_value"]), self.original_bounds, width)
        show = bool(config.get("show", True) and segments and width > 2 and height > 2)
        # Positions are window-content coordinates: native scrolling moves both
        # slider and overlay together without a viewport-coordinate conversion.
        pos = tuple(dpg.get_item_pos(self.slider))
        enabled = self._enabled()
        geometry = (pos, width, height, segments, show, enabled)
        if geometry == self._last_geometry:
            return True
        self._last_geometry = geometry
        dpg.configure_item(self._overlay, show=show, pos=pos)
        if not show:
            return True
        dpg.configure_item(self._drawlist, width=width, height=height)
        # Leave the native frame border visible. A translucent fill preserves
        # the native hover/active background and the slider's grab underneath.
        inset = max(1.0, height / 24.0)
        for index, rectangle in enumerate(self._rectangles):
            if index >= len(segments):
                dpg.configure_item(rectangle, show=False)
                continue
            start, end = segments[index]
            start, end = max(inset, start), min(width - inset, end)
            dpg.configure_item(
                rectangle,
                show=end > start,
                pmin=(start, inset),
                pmax=(end, height - inset),
                fill=(*self._amber, 82 if enabled else 38),
            )
        return True

    def close(self):
        """Remove owned decorations; tolerate a parent/UI already being deleted."""
        if self._closed:
            return
        dpg = self._dpg
        if dpg.does_item_exist(self.input_item) and dpg.get_item_theme(self.input_item) in self._themes:
            previous = self._previous_theme
            dpg.bind_item_theme(self.input_item, previous if previous and dpg.does_item_exist(previous) else 0)
        for item in (self._overlay, self._themes[1]):
            if dpg.does_item_exist(item):
                dpg.delete_item(item)
        self._closed = True
