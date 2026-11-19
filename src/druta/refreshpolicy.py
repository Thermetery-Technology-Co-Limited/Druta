# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Visible-window frame pacing without changing renderer or telemetry policy."""

from __future__ import annotations

import math
import time
from typing import Callable


class FramePacer:
    """Let VSync pace interaction, then limit idle frames after a short grace.

    Call ``note_activity`` when input arrives, ``delay`` before rendering, and
    ``rendered`` after a frame finishes. The caller owns the interruptible wait
    and must check for activity again when it wakes. The injected clock must be
    monotonic. This class neither waits nor changes monitoring or pause state.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.perf_counter,
        idle_fps: float = 10.0,
        active_grace: float = 0.75,
    ) -> None:
        if not math.isfinite(idle_fps) or idle_fps <= 0:
            raise ValueError("idle_fps must be finite and greater than zero")
        if not math.isfinite(active_grace) or active_grace < 0:
            raise ValueError("active_grace must be finite and nonnegative")
        self._clock = clock
        self._idle_interval = 1.0 / idle_fps
        self._active_grace = active_grace
        self._last_activity = clock()
        self._activity_pending = False
        self._last_frame_finished: float | None = None

    def note_activity(self) -> None:
        """Permit an immediate frame and renew the interactive grace period."""
        self._last_activity = self._clock()
        self._activity_pending = True

    def delay(self, continuous: bool = False) -> float:
        """Return the remaining idle wait, or zero for an immediately due frame."""
        now = self._clock()
        if continuous or self._activity_pending or now - self._last_activity < self._active_grace:
            return 0.0
        if self._last_frame_finished is None:
            return 0.0
        return max(0.0, self._last_frame_finished + self._idle_interval - now)

    def rendered(self) -> None:
        """Start the next idle interval at completion; never accumulate debt."""
        self._last_frame_finished = self._clock()
        self._activity_pending = False
