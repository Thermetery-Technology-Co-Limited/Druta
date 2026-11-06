# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Collapsing sections whose nesting is visible.

DearPyGui draws every collapsing header as the same full-width bar at the
left edge, so a subsection was indistinguishable from the section holding it.
Each level here gets its own header colours, and a nested section (bar and
body together) is indented under its parent. DearPyGui applies a header's
theme to its children too, so selectables and combo highlights inside a
section take that level's (muted) header colours; nested sections bind their
own.

Callers pass their own `dpg` module as `ui`, so a test that patches a panel's
module-level dpg also captures the sections that panel draws.
"""

from contextlib import ExitStack, contextmanager

import dearpygui.dearpygui as dpg

INDENT = 18

# (header, hovered, active) per nesting level; deeper levels reuse the last.
_LEVEL_COLOURS = (
    ((38, 66, 104), (48, 82, 128), (58, 98, 150)),
    ((46, 50, 58), (58, 63, 73), (68, 74, 86)),
    ((39, 42, 48), (50, 54, 62), (60, 65, 75)),
)
_themes = {}


def _theme(ui, level):
    level = min(level, len(_LEVEL_COLOURS) - 1)
    key = (id(ui), level)
    theme = _themes.get(key)
    # A new DearPyGui context restarts item ids, so a cached id can name some
    # other item: reuse it only if it is still a theme.
    if theme is not None and ui.does_item_exist(theme) and             str(ui.get_item_info(theme).get("type", "")).endswith("mvTheme"):
        return theme
    header, hovered, active = _LEVEL_COLOURS[level]
    with ui.theme() as theme:
        with ui.theme_component(ui.mvCollapsingHeader):
            ui.add_theme_color(ui.mvThemeCol_Header, header)
            ui.add_theme_color(ui.mvThemeCol_HeaderHovered, hovered)
            ui.add_theme_color(ui.mvThemeCol_HeaderActive, active)
    _themes[key] = theme
    return theme


@contextmanager
def section(label, *, level=0, scale=1.0, note=None, note_color=(139, 144, 153), wrap=900, ui=None, **kwargs):
    """A collapsing header for nesting `level`; yields the header item.

    Level 0 is a top-level section: accent bar, flush left, a gap after it.
    Deeper levels are indented one step under the enclosing body, so the bar
    and its contents sit visibly inside the parent. `note` is a dim line at
    the top of the body, for scope such as "this session only".
    """
    ui = ui or dpg
    with ExitStack() as stack:
        if level:
            stack.enter_context(ui.group(indent=int(round(INDENT * scale))))
        with ui.collapsing_header(label=label, **kwargs) as header:
            ui.bind_item_theme(header, _theme(ui, level))
            if note:
                ui.add_text(note, color=note_color, wrap=int(round(wrap * scale)))
            yield header
        ui.add_spacer(height=int(round((8 if level == 0 else 2) * scale)))
