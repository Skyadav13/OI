"""
Elevated VIX exclusion filter (locked as part of the noise-filter set).
VIX is treated as just another instrument read from the primary data
broker - no special API, same quote-polling mechanism as anything else.
"""
import collections


class VixMonitor:
    def __init__(self, rolling_window: int = 100, elevated_multiplier: float = 1.5):
        self.readings = collections.deque(maxlen=rolling_window)
        self.elevated_multiplier = elevated_multiplier

    def update(self, vix_value: float):
        if vix_value:
            self.readings.append(vix_value)

    def is_elevated(self) -> bool:
        if len(self.readings) < 20:
            return False   # not enough history to judge "elevated" yet - fail open, don't block on no data
        current = self.readings[-1]
        rolling_mean = sum(self.readings) / len(self.readings)
        return current > rolling_mean * self.elevated_multiplier

    def current(self):
        return self.readings[-1] if self.readings else None
