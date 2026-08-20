"""
Minimal config for using oi_engine.py standalone, outside this project.
Every attribute below was verified by grepping oi_engine.py/bar_aggregator.py/
vix_monitor.py directly for self.cfg.* usage - not guessed. Defaults match
the values used and tuned in the original project; treat them as a
starting point, not validated for your specific setup.

IMPORTANT if your bot has only ONE data source (not two brokers):
oi_engine.py's update_futures()/update_option_leg() methods take FOUR
price/OI arguments each (sharekhan_ltp, sharekhan_oi, iifl_ltp, iifl_oi) -
built for this project's dual-broker cross-validation design. With one
data source, pass your single reading for BOTH the "sharekhan_*" and
"iifl_*" parameters on every call, e.g.:
    engine.update_futures(sharekhan_ltp=my_ltp, sharekhan_oi=my_oi,
                          iifl_ltp=my_ltp, iifl_oi=my_oi)
This makes the cross-feed reconciliation trivially agree with itself and
never block - it's a real parameter-shape mismatch to handle, not
something to silently work around differently.

IMPORTANT - the ATR regime filter needs real elapsed wall-clock time:
update_futures() feeds the internal ATR bar-builder using the actual
current time every time you call it (confirmed by testing, not assumed) -
it does not accept a timestamp override through the public API. For LIVE
polling (your real use case - call it once per real poll cycle) this is
completely correct and needs no changes. It DOES mean you can't verify
the ATR filter specifically with a quick synthetic backtest through
update_futures() alone in a few seconds - the filter will legitimately
report "insufficient_atr_history" until enough real polling minutes have
elapsed to build bars. This is expected behavior, not a bug to work around.
"""
from dataclasses import dataclass


@dataclass
class MinimalEngineConfig:
    # --- Buildup classification ---
    lookback_bars: int = 15
    price_thresh_pct: float = 0.15
    oi_thresh_pct: float = 2.0
    min_absolute_oi: float = 500000

    # --- Noise filters ---
    persistence_polls: int = 3
    whipsaw_lookback_polls: int = 20
    whipsaw_max_flips: int = 2
    atr_period: int = 14
    atr_floor_multiplier: float = 0.8
    atr_rolling_mean_polls: int = 50
    gap_open_exclude_minutes: int = 15
    option_leg_staleness_max_polls: int = 3

    # --- Cross-feed reconciliation (see module docstring if single-source) ---
    cross_feed_disagreement_tolerance_pct: float = 5.0

    # --- PCR ---
    pcr_bullish_threshold: float = 1.2
    pcr_bearish_threshold: float = 0.8

    # --- Session timing ---
    high_conf_only_after: str = "13:00"
    no_new_entry_after: str = "14:00"

    # --- Wall detection ---
    strike_step: int = 50
    wall_recompute_minutes: int = 7
