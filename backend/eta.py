"""
eta.py - smoothed estimated-time-remaining tracker.

A naive `elapsed * (1 - progress) / progress` estimate is unreliable here
because the pipeline's stages cost very different amounts of wall-clock
time per unit of progress (color clustering vs. speckle cleanup vs. path
tracing). That makes the ETA jump around every time the job crosses a
stage boundary.

This tracker instead keeps a short rolling window of (time, progress)
samples, computes the recent progress rate from that window, and smooths
the rate with an exponential moving average so the displayed ETA changes
gradually instead of jumping.
"""

from __future__ import annotations

import time
from typing import List, Optional, Tuple


class ETATracker:
    def __init__(self, window_seconds: float = 4.0, smoothing: float = 0.25) -> None:
        self.window_seconds = window_seconds
        self.smoothing = smoothing
        self.samples: List[Tuple[float, float]] = []
        self.smoothed_rate: Optional[float] = None

    def reset(self) -> None:
        self.samples.clear()
        self.smoothed_rate = None

    def update(self, progress: float, now: Optional[float] = None) -> Optional[float]:
        """Record a progress sample and return the current ETA in seconds (or None)."""

        now = time.perf_counter() if now is None else now
        self.samples.append((now, progress))

        cutoff = now - self.window_seconds
        self.samples = [s for s in self.samples if s[0] >= cutoff]

        if len(self.samples) < 2:
            return None

        t0, p0 = self.samples[0]
        dt = now - t0
        dp = progress - p0

        if dt <= 0 or dp <= 0:
            return None

        instant_rate = dp / dt  # progress fraction per second

        if self.smoothed_rate is None:
            self.smoothed_rate = instant_rate
        else:
            self.smoothed_rate = (
                self.smoothing * instant_rate + (1 - self.smoothing) * self.smoothed_rate
            )

        if self.smoothed_rate <= 1e-9:
            return None

        remaining = max(0.0, 1.0 - progress)
        return remaining / self.smoothed_rate
