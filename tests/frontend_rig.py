# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Front-end flows on real, factory-selected backends over one fake driver.

Only the operating system, UI toolkit and persistence boundary are replaced.
The factory gate, backends, profile validators and driver entry points are real.
The clock is fixed so serialized snapshots can be compared byte for byte.
"""
import contextlib
import copy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from druta import druta, native_frontend, profiles, rail_limit_profiles, tuning_profiles
from tests.fake_driver import card
from tests.test_fake_driver import kind_log, merge_static


def frontend_log(kind, log):
    # The Python fake refuses VirtualProtect before a rail splice can install.
    # The native UI test injects precisely that refusal via its hook test seam.
    # Neither reaches a driver write. Retain every actual driver call, removing
    # only the two OS calls of this known refused installation attempt.
    refused = [("kernel32", "GetCurrentThreadId"), ("kernel32", "VirtualProtect")]
    if kind == "python" and log[-2:] == refused:
        assert log[-3] == ("nvapi", "VoltCtrlGet")
        log = log[:-2]
    return kind_log(kind, log)


class Probe:
    """Record absent attributes reached by a front-end getattr/hasattr probe."""
    def __init__(self, gpu):
        self._gpu = gpu
        self.missing = set()

    def __getattr__(self, name):
        try:
            return getattr(self._gpu, name)
        except AttributeError:
            self.missing.add(name)
            raise


@contextlib.contextmanager
def frontend(kind, **options):
    with contextlib.ExitStack() as stack:
        fixed_time = SimpleNamespace(time=lambda: 1700000000.0,
                                     localtime=lambda unused: None,
                                     strftime=lambda *args: "2023-11-14 22:13:20")
        for module in (profiles, tuning_profiles, rail_limit_profiles):
            stack.enter_context(patch.object(module, "time", fixed_time))
        native_frontend.reset_missing_members_for_tests()
        session = stack.enter_context(card(kind, **options))
        merge_static(session)
        session.gpu.arch()
        # The GUI's lifetime state is genuine; DPG and disk preferences are external seams.
        ui = MagicMock()
        values = {"unlock": True}
        ui.does_item_exist.side_effect = lambda name: name in values
        ui.get_value.side_effect = values.get
        ui.set_value.side_effect = values.__setitem__
        stack.enter_context(patch.object(druta, "dpg", ui))
        stack.enter_context(patch.object(druta.shuntmod, "load", return_value=[]))
        stack.enter_context(patch.object(druta.vfheadroom, "load", return_value=(False, 0)))
        stack.enter_context(patch.object(druta, "dpi_scale", return_value=1))
        # Reuse the already opened card, as the real constructor would obtain it from the factory.
        # Construction itself is tested separately with an unpatched factory entry point.
        with patch.object(druta, "open_gpu", return_value=session.gpu):
            app = druta.Druta(session.gpu.slot())
        saved, messages = [], []
        app.log = lambda message, ok=None: messages.append((message, ok))
        app.report = lambda result: (messages.append(result), result)[1]

        def autosave(label, state):
            saved.append((label, copy.deepcopy(state)))
            return "fake-undo", None, profiles.incomplete(state)
        stack.enter_context(patch.object(profiles, "autosave_state", side_effect=autosave))
        session.app, session.values = app, values
        session.saved, session.messages = saved, messages
        session.driver.clear_part_logs()
        session.start = session.driver.mark()
        yield session


def run(kind, action, **options):
    with frontend(kind, **options) as session:
        result = action(session)
        assert session.driver.errors == []
        if kind == "native":
            assert native_frontend.drain_missing_members() == []
        return {"result": result,
                "log": frontend_log(kind, session.driver.since(session.start)),
                "state": session.driver.state(),
                "parts": session.driver.part_logs(),
                "saved": session.saved, "messages": session.messages}


def both(kind, action, **options):
    observed = run(kind, action, **options)
    if kind == "native":
        expected = run("python", action, **options)
        for key in observed:
            assert observed[key] == expected[key], key
    return observed
