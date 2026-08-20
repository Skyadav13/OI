"""
Elevated VIX exclusion filter (locked as part of the noise-filter set).
VIX is treated as just another instrument read from the primary data
broker - no special API, same quote-polling mechanism as anything else.

CORRECTED behavior for missing VIX data - a real gap fixed here, not just
a style choice. Previously, missing data always failed open (treated as
"not elevated," entries proceeded normally) with no time limit at all -
inconsistent with every other missing-data case in this design (whipsaw
guard, option-leg staleness, dual-feed outage all block/degrade rather
than silently assume "fine"). VIX is exactly the filter meant to catch
event-driven volatility days, which is also when a data feed is more
likely to hiccup - failing open indefinitely loses that protection right
when it matters most.

New behavior: short gaps still fail open (a transient blip shouldn't halt
an otherwise fine day), but a SUSTAINED gap (default 30 minutes) blocks
new entries - same escalation pattern as feed_health/degraded_mode's
staleness tracking elsewhere in this project.
"""
import time
import collections


class VixMonitor:
    def __init__(self, rolling_window: int = 100, elevated_multiplier: float = 1.5,
                 stale_block_seconds: int = 1800):
        self.readings = collections.deque(maxlen=rolling_window)
        self.elevated_multiplier = elevated_multiplier
        self.stale_block_seconds = stale_block_seconds
        self.last_update_ts = None
        self.created_ts = time.time()

    def update(self, vix_value: float):
        if vix_value:
            self.readings.append(vix_value)
            self.last_update_ts = time.time()

    def is_elevated(self) -> bool:
        if len(self.readings) < 20:
            return False   # not enough history to judge "elevated" yet - fail open, don't block on no data
        current = self.readings[-1]
        rolling_mean = sum(self.readings) / len(self.readings)
        return current > rolling_mean * self.elevated_multiplier

    def seconds_since_last_update(self) -> float:
        """Measured from last successful update, or from monitor creation
        if VIX has never arrived at all in this session - same threshold
        covers both "was fine, now stopped" and "never got data" cases."""
        anchor = self.last_update_ts if self.last_update_ts is not None else self.created_ts
        return time.time() - anchor

    def is_data_stale(self) -> bool:
        return self.seconds_since_last_update() > self.stale_block_seconds

    def should_block_entries(self) -> bool:
        """The single call site should use - combines genuine elevation
        with the sustained-missing-data case. True means: do not enter."""
        return self.is_elevated() or self.is_data_stale()

    def current(self):
        return self.readings[-1] if self.readings else None
