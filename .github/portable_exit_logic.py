"""
Portable exit-priority logic - extracted from trade_manager.py/oi_engine.py,
stripped of every dependency on this project's Config/State/Position/broker
classes. Pure functions and one small dataclass, safe to drop into any
existing bot's codebase directly (no imports from this project needed).

Exit priority (unchanged from the locked design): catastrophic backstop
> OI-reversal > trailing-on-halfway > wall target.

Two bugs were caught and fixed during this project's own integration
testing, both preserved here as the correct behavior:
  1. A wall level is a STRIKE PRICE (on the underlying), never an option
     PREMIUM - compute_wall_target() translates one to the other. Comparing
     them directly is a real bug, not a style choice.
  2. If the translated distance comes out <= 0 (the wall landed at/behind
     spot), fall back to a percentage-based target rather than collapsing
     to entry_price, which causes an instant false "target hit" exit.
"""
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------
# Entry-side building block, reused here because OI-reversal (an exit
# rule) needs the same buildup classification the entry signal uses.
# ---------------------------------------------------------------------
def classify_buildup(price0: float, price1: float, oi0: float, oi1: float,
                      price_thresh_pct: float, oi_thresh_pct: float) -> str:
    """Returns one of: LONG_BUILDUP, SHORT_BUILDUP, SHORT_COVERING,
    LONG_UNWINDING, NEUTRAL."""
    if not price0 or not oi0:
        return "NEUTRAL"
    price_chg = (price1 - price0) / price0 * 100
    oi_chg = (oi1 - oi0) / oi0 * 100

    if abs(price_chg) < price_thresh_pct or abs(oi_chg) < oi_thresh_pct:
        return "NEUTRAL"
    if price_chg > 0 and oi_chg > 0:
        return "LONG_BUILDUP"
    if price_chg < 0 and oi_chg > 0:
        return "SHORT_BUILDUP"
    if price_chg > 0 and oi_chg < 0:
        return "SHORT_COVERING"
    return "LONG_UNWINDING"


def bias_of(classification: str) -> str:
    if classification in ("LONG_BUILDUP", "SHORT_COVERING"):
        return "BULLISH"
    if classification in ("SHORT_BUILDUP", "LONG_UNWINDING"):
        return "BEARISH"
    return "NONE"


def persistent_bias(classification_history: list, needed: int) -> str:
    """classification_history: list of classification strings (not tuples),
    oldest first. Returns BULLISH/BEARISH only if the last `needed` entries
    all agree; NONE otherwise."""
    if len(classification_history) < needed:
        return "NONE"
    recent = classification_history[-needed:]
    biases = [bias_of(c) for c in recent]
    if all(b == "BULLISH" for b in biases):
        return "BULLISH"
    if all(b == "BEARISH" for b in biases):
        return "BEARISH"
    return "NONE"


# ---------------------------------------------------------------------
# Wall-to-premium translation (regression-tested bug fix, see docstring)
# ---------------------------------------------------------------------
def compute_wall_target(wall_strike: Optional[float], current_spot: Optional[float],
                         entry_premium: float, direction: str,
                         assumed_delta: float = 0.5,
                         fallback_multiplier: float = 1.6) -> float:
    """direction: "BULLISH" or "BEARISH". Returns a premium-scale target,
    never a strike-scale number and never equal to entry_premium."""
    if wall_strike is None or current_spot is None:
        return entry_premium * fallback_multiplier if direction == "BULLISH" \
            else entry_premium * (2 - fallback_multiplier)

    underlying_distance = (wall_strike - current_spot) if direction == "BULLISH" \
        else (current_spot - wall_strike)

    if underlying_distance <= 0:
        return entry_premium * fallback_multiplier if direction == "BULLISH" \
            else entry_premium * (2 - fallback_multiplier)

    return entry_premium + underlying_distance * assumed_delta


# ---------------------------------------------------------------------
# Exit stack
# ---------------------------------------------------------------------
@dataclass
class ExitState:
    """Carry this alongside your own position object - one instance per
    open trade, reset when a new position opens."""
    trailing_active: bool = False
    trail_stop: Optional[float] = None


def evaluate_exit(direction: str, entry_price: float, current_price: float,
                   catastrophic_stop_pct: float,
                   classification_history_since_entry: list, reversal_confirm_polls: int,
                   wall_target_price: float,
                   trail_activate_at_pct_of_target: float, trail_lock_fraction: float,
                   exit_state: ExitState) -> tuple:
    """Returns (should_exit: bool, reason: str|None, exit_state: ExitState).
    exit_state is mutated in place AND returned for convenience - keep
    passing the same instance back in on every poll for one open trade.

    Priority order, matching the locked design exactly:
      1. catastrophic backstop (always checked first)
      2. OI-reversal (rebase classification_history_since_entry to your
         fill time - only pass classifications from AFTER entry)
      3. trailing-on-halfway / wall target
    """
    catastrophic_price = (entry_price * (1 - catastrophic_stop_pct / 100) if direction == "BULLISH"
                          else entry_price * (1 + catastrophic_stop_pct / 100))
    if (direction == "BULLISH" and current_price <= catastrophic_price) or \
       (direction == "BEARISH" and current_price >= catastrophic_price):
        return True, "CATASTROPHIC", exit_state

    reversal_needed = "BEARISH" if direction == "BULLISH" else "BULLISH"
    confirmed = persistent_bias(classification_history_since_entry, reversal_confirm_polls)
    if confirmed == reversal_needed:
        return True, "OI_REVERSAL", exit_state

    target_distance = wall_target_price - entry_price   # positive for BULLISH, negative for BEARISH
    activation_point = entry_price + trail_activate_at_pct_of_target * target_distance
    reached_halfway = (current_price >= activation_point) if direction == "BULLISH" else (current_price <= activation_point)
    if reached_halfway and not exit_state.trailing_active:
        exit_state.trailing_active = True

    if exit_state.trailing_active:
        gain = (current_price - entry_price) if direction == "BULLISH" else (entry_price - current_price)
        locked = entry_price + trail_lock_fraction * gain if direction == "BULLISH" \
            else entry_price - trail_lock_fraction * gain
        exit_state.trail_stop = (max(exit_state.trail_stop, locked) if exit_state.trail_stop is not None
                                 else locked) if direction == "BULLISH" else \
                                (min(exit_state.trail_stop, locked) if exit_state.trail_stop is not None
                                 else locked)
        if (direction == "BULLISH" and current_price <= exit_state.trail_stop) or \
           (direction == "BEARISH" and current_price >= exit_state.trail_stop):
            return True, "TRAIL_STOP", exit_state

    if (direction == "BULLISH" and current_price >= wall_target_price) or \
       (direction == "BEARISH" and current_price <= wall_target_price):
        return True, "TARGET", exit_state

    return False, None, exit_state
                     
