"""
Core OI signal engine. Every evaluate() call runs the complete locked
filter chain and returns a SignalResult with pass/fail plus the specific
reason for a block - this result is what feeds directly into the daily
report's signal audit (true/false signal log), not just the final trade
decision.

Cross-feed handling: Sharekhan (primary) and IIFL (secondary) readings are
reconciled on every update. Disagreement beyond tolerance is treated the
same as the whipsaw guard - blocks the signal, does not pick one feed as
"more correct" arbitrarily, per locked design.
"""
import time
import collections
import logging
from dataclasses import dataclass
from typing import Optional

from bar_aggregator import BarAggregator

logger = logging.getLogger("oi_engine")

Classification = str   # "LONG_BUILDUP" | "SHORT_BUILDUP" | "SHORT_COVERING" | "LONG_UNWINDING" | "NEUTRAL"


@dataclass
class SignalResult:
    timestamp: float
    direction: str = "NONE"          # "BULLISH" | "BEARISH" | "NONE"
    passed: bool = False             # True/False for the signal audit log
    block_reason: Optional[str] = None
    buildup: Optional[str] = None
    price_chg_pct: Optional[float] = None
    oi_chg_pct: Optional[float] = None
    pcr: Optional[float] = None
    pcr_bias: Optional[str] = None
    conviction_score: float = 0.0
    atm_strike: Optional[int] = None
    feed_disagreement: bool = False


class OIEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.fut_history = collections.deque(maxlen=500)   # (ts, ltp, oi) reconciled
        self.classification_history = collections.deque(maxlen=200)
        self.bar_agg = BarAggregator(bar_seconds=60)
        self.option_leg_state = {}   # {(strike, type): {"oi": float, "unchanged_polls": int}}
        self.wall_cache = {"computed_at": 0, "call_wall": None, "put_wall": None}
        self.session_start_ts = None

    def mark_session_start(self, ts: float = None):
        self.session_start_ts = ts or time.time()

    # ---------- cross-feed reconciliation ----------
    def _reconcile(self, primary_val, secondary_val, label: str):
        """Returns (reconciled_value, disagreement_flag). Reconciled value
        prefers primary (Sharekhan) when both present and in agreement;
        None if they disagree beyond tolerance - caller must not trade on it."""
        if primary_val is None and secondary_val is None:
            return None, False
        if primary_val is None:
            return secondary_val, False   # primary down, secondary covers (degraded but not blocked)
        if secondary_val is None:
            return primary_val, False

        if primary_val == 0:
            return primary_val, False
        divergence_pct = abs(primary_val - secondary_val) / abs(primary_val) * 100
        if divergence_pct > self.cfg.cross_feed_disagreement_tolerance_pct:
            logger.warning("Feed disagreement on %s: sharekhan=%.2f iifl=%.2f (%.1f%% apart)",
                           label, primary_val, secondary_val, divergence_pct)
            return None, True
        return primary_val, False

    def update_futures(self, sharekhan_ltp, sharekhan_oi, iifl_ltp, iifl_oi) -> bool:
        """Returns True if a usable reconciled reading was recorded."""
        ltp, ltp_disagree = self._reconcile(sharekhan_ltp, iifl_ltp, "futures_ltp")
        oi, oi_disagree = self._reconcile(sharekhan_oi, iifl_oi, "futures_oi")

        if ltp is not None:
            self.bar_agg.add_tick(ltp)

        if ltp is None or oi is None:
            return False
        self.fut_history.append((time.time(), ltp, oi))
        return True

    def update_option_leg(self, strike: int, option_type: str, sharekhan_oi, iifl_oi):
        oi, disagree = self._reconcile(sharekhan_oi, iifl_oi, f"{strike}{option_type}_oi")
        key = (strike, option_type)
        state = self.option_leg_state.setdefault(key, {"oi": None, "unchanged_polls": 0})
        if oi is None:
            return
        if state["oi"] is not None and oi == state["oi"]:
            state["unchanged_polls"] += 1
        else:
            state["unchanged_polls"] = 0
        state["oi"] = oi

    def is_option_leg_stale(self, strike: int, option_type: str) -> bool:
        state = self.option_leg_state.get((strike, option_type))
        if not state:
            return True
        return state["unchanged_polls"] >= self.cfg.option_leg_staleness_max_polls

    def get_option_oi(self, strike: int, option_type: str):
        state = self.option_leg_state.get((strike, option_type))
        return state["oi"] if state else None

    # ---------- futures buildup classification ----------
    def _window(self, dq, n):
        return list(dq)[-n:] if len(dq) >= n else None

    def classify_futures_buildup(self):
        window = self._window(self.fut_history, self.cfg.lookback_bars)
        if not window or len(window) < 2:
            return None
        _, price0, oi0 = window[0]
        _, price1, oi1 = window[-1]
        if not price0 or not oi0:
            return None

        price_chg = (price1 - price0) / price0 * 100
        oi_chg = (oi1 - oi0) / oi0 * 100

        if abs(price_chg) < self.cfg.price_thresh_pct or abs(oi_chg) < self.cfg.oi_thresh_pct:
            return "NEUTRAL", price_chg, oi_chg
        if price_chg > 0 and oi_chg > 0:
            return "LONG_BUILDUP", price_chg, oi_chg
        if price_chg < 0 and oi_chg > 0:
            return "SHORT_BUILDUP", price_chg, oi_chg
        if price_chg > 0 and oi_chg < 0:
            return "SHORT_COVERING", price_chg, oi_chg
        return "LONG_UNWINDING", price_chg, oi_chg

    @staticmethod
    def _bias(classification: str) -> str:
        if classification in ("LONG_BUILDUP", "SHORT_COVERING"):
            return "BULLISH"
        if classification in ("SHORT_BUILDUP", "LONG_UNWINDING"):
            return "BEARISH"
        return "NONE"

    def _persistent_bias(self, needed: int) -> str:
        if len(self.classification_history) < needed:
            return "NONE"
        recent = list(self.classification_history)[-needed:]
        biases = [self._bias(c[0]) for c in recent if c is not None]
        if len(biases) < needed:
            return "NONE"
        if all(b == "BULLISH" for b in biases):
            return "BULLISH"
        if all(b == "BEARISH" for b in biases):
            return "BEARISH"
        return "NONE"

    def persistent_bias_since(self, start_index: int, needed: int) -> str:
        """Same persistence logic as _persistent_bias, but scoped to history
        from start_index onward - used by the OI-reversal exit stop, which
        must be rebased to fill time, not evaluated against the full
        rolling window. Per locked design."""
        history = list(self.classification_history)[start_index:]
        if len(history) < needed:
            return "NONE"
        recent = history[-needed:]
        biases = [self._bias(c[0]) for c in recent if c is not None]
        if len(biases) < needed:
            return "NONE"
        if all(b == "BULLISH" for b in biases):
            return "BULLISH"
        if all(b == "BEARISH" for b in biases):
            return "BEARISH"
        return "NONE"

    def _whipsaw_blocked(self) -> bool:
        window = list(self.classification_history)[-self.cfg.whipsaw_lookback_polls:]
        biases = [self._bias(c[0]) for c in window if c is not None and self._bias(c[0]) != "NONE"]
        flips = sum(1 for a, b in zip(biases, biases[1:]) if a != b)
        return flips > self.cfg.whipsaw_max_flips

    # ---------- PCR ----------
    @staticmethod
    def compute_pcr(call_ois: dict, put_ois: dict):
        total_call = sum(v for v in call_ois.values() if v is not None)
        total_put = sum(v for v in put_ois.values() if v is not None)
        if total_call == 0:
            return None
        return total_put / total_call

    def pcr_bias(self, pcr):
        if pcr is None:
            return "NONE"
        if pcr >= self.cfg.pcr_bullish_threshold:
            return "BULLISH"
        if pcr <= self.cfg.pcr_bearish_threshold:
            return "BEARISH"
        return "NONE"

    # ---------- OI wall ----------
    def compute_wall(self, atm_strike: int, call_ois: dict, put_ois: dict, entry_strike: int,
                      current_spot: float = None):
        """Peak-OI wall, one strike short of the peak, with the
        entry-strike degenerate-case fallback, per locked design.
        A call wall (resistance) must sit ABOVE current spot; a put wall
        (support) must sit BELOW it - a wall on the wrong side of spot
        isn't a usable target (caught during integration testing, where an
        unfiltered candidate set picked a "resistance" below spot)."""
        now = time.time()
        if now - self.wall_cache["computed_at"] < self.cfg.wall_recompute_minutes * 60:
            return self.wall_cache["call_wall"], self.wall_cache["put_wall"]

        spot = current_spot if current_spot is not None else atm_strike
        call_candidates = {k: v for k, v in call_ois.items() if k > spot}
        put_candidates = {k: v for k, v in put_ois.items() if k < spot}

        call_wall = self._nearest_wall(call_candidates, entry_strike, self.cfg.strike_step, direction=1)
        put_wall = self._nearest_wall(put_candidates, entry_strike, self.cfg.strike_step, direction=-1)

        self.wall_cache = {"computed_at": now, "call_wall": call_wall, "put_wall": put_wall}
        return call_wall, put_wall

    @staticmethod
    def _nearest_wall(ois: dict, entry_strike: int, step: int, direction: int):
        candidates = {k: v for k, v in ois.items() if v is not None and k != entry_strike}
        if not candidates:
            return None
        peak_strike = max(candidates, key=candidates.get)
        return peak_strike - (direction * step)   # one strike short of the peak

    # ---------- conviction score ----------
    def conviction_score(self, price_chg_pct: float, oi_chg_pct: float) -> float:
        price_ratio = abs(price_chg_pct) / self.cfg.price_thresh_pct if self.cfg.price_thresh_pct else 1
        oi_ratio = abs(oi_chg_pct) / self.cfg.oi_thresh_pct if self.cfg.oi_thresh_pct else 1
        return round(price_ratio * oi_ratio, 2)

    # ---------- full evaluation (the signal audit row) ----------
    def evaluate(self, current_oi: float, time_str: str, call_ois: dict, put_ois: dict,
                 vix_blocked: bool, atm_strike: int, vix_stale: bool = False) -> SignalResult:
        ts = time.time()
        result = SignalResult(timestamp=ts, atm_strike=atm_strike)

        buildup_result = self.classify_futures_buildup()
        self.classification_history.append(buildup_result)

        if buildup_result is None:
            result.block_reason = "insufficient_history"
            return result

        classification, price_chg, oi_chg = buildup_result
        result.buildup = classification
        result.price_chg_pct = round(price_chg, 3)
        result.oi_chg_pct = round(oi_chg, 3)
        result.conviction_score = self.conviction_score(price_chg, oi_chg)

        if self.session_start_ts and (ts - self.session_start_ts) < self.cfg.gap_open_exclude_minutes * 60:
            result.block_reason = "gap_open_window"
            return result

        if current_oi is None or current_oi < self.cfg.min_absolute_oi:
            result.block_reason = "oi_floor"
            return result

        atr = self.bar_agg.atr(self.cfg.atr_period)
        atr_mean = self.bar_agg.atr_rolling_mean(self.cfg.atr_period, self.cfg.atr_rolling_mean_polls)
        if atr is None or atr_mean is None:
            result.block_reason = "insufficient_atr_history"
            return result
        if atr < self.cfg.atr_floor_multiplier * atr_mean:
            result.block_reason = "atr_regime_too_quiet"
            return result

        if self._whipsaw_blocked():
            result.block_reason = "whipsaw_guard"
            return result

        if vix_blocked:
            result.block_reason = "vix_data_stale" if vix_stale else "vix_elevated"
            return result

        fut_bias = self._persistent_bias(self.cfg.persistence_polls)
        if fut_bias == "NONE":
            result.block_reason = "persistence_not_confirmed"
            return result

        pcr = self.compute_pcr(call_ois, put_ois)
        result.pcr = round(pcr, 3) if pcr is not None else None
        pcr_bias = self.pcr_bias(pcr)
        result.pcr_bias = pcr_bias

        # option-leg staleness at the ATM strike specifically (the leg we'd actually trade)
        ce_stale = self.is_option_leg_stale(atm_strike, "CE")
        pe_stale = self.is_option_leg_stale(atm_strike, "PE")
        if ce_stale and pe_stale:
            result.block_reason = "option_leg_stale"
            return result

        if pcr_bias == "NONE":
            result.block_reason = "pcr_neutral"
            return result

        if fut_bias != pcr_bias:
            result.block_reason = "futures_pcr_disagree"
            return result

        if time_str >= self.cfg.no_new_entry_after:
            result.block_reason = "past_no_entry_cutoff"
            return result

        if time_str >= self.cfg.high_conf_only_after:
            strong_enough = (abs(price_chg) >= 2 * self.cfg.price_thresh_pct and
                              abs(oi_chg) >= 2 * self.cfg.oi_thresh_pct)
            if not strong_enough:
                result.block_reason = "tiered_window_needs_high_confidence"
                return result

        result.direction = fut_bias
        result.passed = True
        return result
