"""
The bot polls LTP rather than receiving OHLC candles directly, but the ATR
volatility-regime filter needs true-range bars. This bins incoming LTP
readings into fixed-duration bars (default 1 minute) to build a rolling
OHLC series good enough for ATR - not a substitute for real market data,
just what's needed to compute the one indicator that requires it.
"""
import time
import collections


class BarAggregator:
    def __init__(self, bar_seconds: int = 60, max_bars: int = 200):
        self.bar_seconds = bar_seconds
        self.bars = collections.deque(maxlen=max_bars)
        self._current_bucket = None
        self._current_bar = None   # {"open","high","low","close"}

    def add_tick(self, ltp: float, ts: float = None):
        ts = ts or time.time()
        bucket = int(ts // self.bar_seconds)

        if self._current_bucket is None:
            self._current_bucket = bucket
            self._current_bar = {"open": ltp, "high": ltp, "low": ltp, "close": ltp}
            return

        if bucket == self._current_bucket:
            self._current_bar["high"] = max(self._current_bar["high"], ltp)
            self._current_bar["low"] = min(self._current_bar["low"], ltp)
            self._current_bar["close"] = ltp
        else:
            self.bars.append(self._current_bar)
            self._current_bucket = bucket
            self._current_bar = {"open": ltp, "high": ltp, "low": ltp, "close": ltp}

    def true_range_series(self):
        all_bars = list(self.bars) + ([self._current_bar] if self._current_bar else [])
        trs = []
        prev_close = None
        for bar in all_bars:
            if prev_close is None:
                trs.append(bar["high"] - bar["low"])
            else:
                trs.append(max(
                    bar["high"] - bar["low"],
                    abs(bar["high"] - prev_close),
                    abs(bar["low"] - prev_close),
                ))
            prev_close = bar["close"]
        return trs

    def atr(self, period: int):
        trs = self.true_range_series()
        if len(trs) < period:
            return None
        return sum(trs[-period:]) / period

    def atr_rolling_mean(self, period: int, mean_bars: int):
        trs = self.true_range_series()
        if len(trs) < period + mean_bars:
            return None
        atrs = []
        for i in range(len(trs) - mean_bars, len(trs)):
            if i < period - 1:
                continue
            atrs.append(sum(trs[i - period + 1:i + 1]) / period)
        return sum(atrs) / len(atrs) if atrs else None
