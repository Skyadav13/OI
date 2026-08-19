"""
Hard sanity bounds - locked design requirement: applies to every parameter
change regardless of whether it came from a manual APPLY command or a
weekly analytics recommendation. Catches a fat-fingered or reasoning-error
value before it reaches something that trades real money.
"""
PARAM_BOUNDS = {
    "price_thresh_pct": (0.05, 1.0),
    "oi_thresh_pct": (0.5, 10.0),
    "persistence_polls": (1, 6),
    "min_absolute_oi": (50000, 5000000),
    "whipsaw_max_flips": (0, 5),
    "atr_floor_multiplier": (0.3, 1.5),
    "pcr_bullish_threshold": (1.0, 2.0),
    "pcr_bearish_threshold": (0.4, 1.0),
    "reversal_confirm_polls": (1, 5),
    "catastrophic_stop_pct": (15.0, 70.0),
    "cooldown_minutes_after_stop": (5, 60),
    "max_trades_per_day": (1, 10),
    "daily_loss_limit_pct": (1.0, 8.0),
    "conviction_max_lots": (1, 5),
}


def validate(param: str, value):
    if param not in PARAM_BOUNDS:
        return False, f"'{param}' is not a recognized tunable parameter."
    lo, hi = PARAM_BOUNDS[param]
    if not (lo <= value <= hi):
        return False, f"'{param}'={value} is outside the allowed range [{lo}, {hi}]."
    return True, None
