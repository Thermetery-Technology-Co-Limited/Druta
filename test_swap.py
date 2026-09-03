# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""In-place card switching, exercised against real hardware.

Not a unit test: it builds the actual window, renders frames, swaps cards
through the same callback the menu uses, and then asks the WIDGETS what they
say. The point is that a switch must leave no control carrying the previous
card's numbers, and the only way to be sure of that is to read them back.

Needs two NVIDIA GPUs and, for the V/F table probe, administrator rights.
Run: python test_swap.py
"""
import sys

import dearpygui.dearpygui as dpg

import druta
import nvbackend


FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}"
          + ("" if ok else f", want {want!r}"))
    if not ok:
        FAILS.append(label)
    return ok


def differs(label, a, b):
    """Assert two cards genuinely disagree, so a passing equality check below
    is evidence of a rebuild and not of the two cards happening to match."""
    ok = a != b
    print(f"  {'PASS' if ok else 'SKIP'}  {label} differs across cards: "
          f"{a!r} vs {b!r}")
    if not ok:
        print("        (this check cannot prove anything on these two cards)")
    return ok


def widget(tag, key):
    return dpg.get_item_configuration(tag)[key]


def snapshot_ui():
    """What the window currently claims, read out of the widgets themselves."""
    return {
        "vf_set_max": widget("vf_set", "max_value"),
        "vf_set_min": widget("vf_set", "min_value"),
        "vf_set_step": widget("vf_set", "step"),
        "lock_min": widget("lock_min", "min_value"),
        "lock_max": widget("lock_max", "max_value"),
        "lock_step": widget("lock_min", "step"),
    }


def frames(n=3):
    for _ in range(n):
        if dpg.is_dearpygui_running():
            dpg.render_dearpygui_frame()


def main():
    cards = nvbackend.enumerate_gpus()
    if len(cards) < 2:
        print(f"need two GPUs, found {len(cards)}")
        return 1
    a, b = cards[0]["slot"], cards[1]["slot"]
    print(f"card A {a}  {cards[0]['name']}")
    print(f"card B {b}  {cards[1]['name']}\n")

    app = druta.Druta(a)
    dpg.create_context()
    dpg.create_viewport(title="swap test", width=1180, height=900)
    app.load_fonts()
    with dpg.window(tag="root"):
        dpg.add_spacer(tag="menu_pad", height=app.menu_h())
    app.build_ui()
    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window("root", True)
    app.relayout()
    frames()

    expect_a = {"gfx_min": app.gpu.static.get("gfx_min"),
                "gfx_max": app.gpu.static.get("gfx_max"),
                "step": app.step_mhz()}
    ui_a = snapshot_ui()
    app.vf_read()
    n_rows_a = app.n_vf_rows()
    print(f"on A: {app.gpu.static['name']}  gfx {expect_a['gfx_min']}-"
          f"{expect_a['gfx_max']}  step {expect_a['step']}  "
          f"{n_rows_a} V/F rows")
    print(f"  widgets: {ui_a}\n")

    # ---- swap A -> B ------------------------------------------------------ #
    print(f"swap -> {b}")
    assert app.swap_gpu(b), "swap_gpu returned False"
    frames()
    expect_b = {"gfx_min": app.gpu.static.get("gfx_min"),
                "gfx_max": app.gpu.static.get("gfx_max"),
                "step": app.step_mhz()}
    ui_b = snapshot_ui()
    print(f"on B: {app.gpu.static['name']}  gfx {expect_b['gfx_min']}-"
          f"{expect_b['gfx_max']}  step {expect_b['step']}")
    print(f"  widgets: {ui_b}\n")

    print("the two cards must actually differ, or these checks prove nothing:")
    differs("gfx_max", expect_a["gfx_max"], expect_b["gfx_max"])
    differs("clock bin", expect_a["step"], expect_b["step"])
    print()

    print("widgets rebuilt against card B:")
    check("vf_set max", ui_b["vf_set_max"], expect_b["gfx_max"])
    check("vf_set min", ui_b["vf_set_min"], expect_b["gfx_min"])
    check("vf_set step", ui_b["vf_set_step"], expect_b["step"])
    check("lock_min bound", ui_b["lock_min"], expect_b["gfx_min"])
    check("lock_max bound", ui_b["lock_max"], expect_b["gfx_max"])
    check("lock step", ui_b["lock_step"], expect_b["step"])
    check("selected slot", app.gpu.slot(), b)
    check("generation bumped", app._gpu_gen, 1)
    print()

    print("per-card state dropped:")
    # vf_work is NOT empty here and should not be: swap_gpu ends with a
    # vf_read, so it holds the new card's curve at zero deltas. The two things
    # worth asserting are that it carries no staged EDIT (work == orig) and
    # that it was re-read at the new card's size - 80 rows on GP102 where the
    # outgoing TU102 had 128, which is the check that would catch a working
    # copy carried across intact.
    check("no staged edit pending", app.vf_work, app.vf_orig)
    check("working copy re-sized to this card",
          len(app.vf_work), app.n_vf_rows())
    differs("V/F row count", n_rows_a, app.n_vf_rows())
    check("undo history", app._undo, [])
    check("redo history", app._redo, [])
    check("plan note", app._plan_note, None)
    check("lockable cache", app._lockable, None)
    check("timing snapshot", app._tim, None)
    check("staged timing writes", app._tw_pending, {})
    check("drawn-name cache", app._dom_name, {})
    print()

    # ---- swap back B -> A, and confirm it restores exactly ---------------- #
    print(f"swap back -> {a}")
    assert app.swap_gpu(a), "swap back returned False"
    frames()
    ui_a2 = snapshot_ui()
    print(f"  widgets: {ui_a2}\n")
    print("returning to card A restores card A's widgets:")
    check("round trip", ui_a2, ui_a)
    check("slot", app.gpu.slot(), a)
    check("generation", app._gpu_gen, 2)
    print()

    # a staged edit must block the first click and go through on the second
    print("staged edits arm the switch rather than vanishing:")
    app.vf_work = {5: 3}
    app.switch_gpu(user_data=b)
    check("first click refused", app.gpu.slot(), a)
    check("armed", app._switch_armed, b)
    app.switch_gpu(user_data=b)
    check("second click switched", app.gpu.slot(), b)
    print()

    # a held lock must refuse outright, however many times it is clicked
    print("a held lock refuses the switch:")
    app._clk_lock = {"kind": app.LOCK_NVML}
    app.switch_gpu(user_data=a)
    app.switch_gpu(user_data=a)
    check("still on B", app.gpu.slot(), b)
    app._clk_lock = None
    print()

    # ---- leaks: a switch that grows the item tree would eventually die ---- #
    print("repeated swaps must not grow the item tree:")
    app.swap_gpu(a)
    frames()
    before = len(dpg.get_all_items())
    for i in range(6):
        app.swap_gpu(b if i % 2 == 0 else a)
        frames(1)
    after = len(dpg.get_all_items())
    print(f"  items {before} -> {after} after 6 swaps "
          f"({(after - before) / 6:+.1f} per swap)")
    check("no item leak", after, before)

    # The memory cost was the least of it. A second live handler registry means
    # every key handler fires twice, so one press of W nudges the selected
    # point two bins and one Ctrl+Z walks back two edits. This is the check
    # that the editor still behaves, not just that the process stays small.
    kinds = {}
    for i in dpg.get_all_items():
        try:
            kinds[dpg.get_item_type(i)] = kinds.get(dpg.get_item_type(i), 0) + 1
        except Exception:
            pass
    regs = kinds.get("mvAppItemType::mvHandlerRegistry", 0)
    keys = kinds.get("mvAppItemType::mvKeyPressHandler", 0)
    print(f"  handler registries {regs}, key handlers {keys}")
    check("exactly one handler registry", regs, 1)
    check("key handlers not duplicated", keys, 7)
    print()

    # ---- a capture in flight when the card changes ------------------------ #
    print("a capture that started on the old card is dropped, not filed:")
    app.swap_gpu(a)
    frames()
    gen_at_start = app._gpu_gen
    app._tim = None
    app._tim_busy = True

    class _Stale:
        ok = mem_stable = True
        key = 9999
        codename = "STALE"

    # what the worker's tail does, with the stamp it took on entry
    def file_result(gen, snap):
        with app._tim_lock:
            if gen != app._gpu_gen:
                app._tim_busy = False
                return "dropped"
            app._tim = snap
            app._tim_busy = False
            return "filed"

    app.swap_gpu(b)          # card changes while the "worker" is running
    frames()
    outcome = file_result(gen_at_start, _Stale())
    check("stale capture dropped", outcome, "dropped")
    check("timings tab not poisoned",
          getattr(app._tim, "codename", None) != "STALE", True)
    print()

    frames()
    dpg.destroy_context()
    print("=" * 60)
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
