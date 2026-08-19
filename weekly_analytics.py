"""
Weekly cadence (not daily - locked design, avoids overfitting to small
samples). Rolling window minimum 15-20 trading days. Produces
recommendations only - never auto-applied. Application requires an
explicit Telegram APPLY command, bounds-checked, fully audit-trailed.
"""
import csv
import os
import datetime
import logging

from param_bounds import validate
from config_overrides import save_override

logger = logging.getLogger("weekly_analytics")

MIN_TRADING_DAYS = 15


def _read_csv_rows(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def _trading_days_covered(rows, time_field="entry_time"):
    days = set()
    for r in rows:
        raw = r.get(time_field)
        if not raw:
            continue
        try:
            ts = float(raw)
            days.add(datetime.datetime.fromtimestamp(ts).date().isoformat())
        except (ValueError, TypeError):
            pass
    return days


def analyze(cfg) -> dict:
    trade_path = cfg.live_trade_log if os.path.exists(cfg.live_trade_log) else cfg.paper_trade_log
    trades = _read_csv_rows(trade_path)
    audit = _read_csv_rows(cfg.signal_audit_log)

    days_covered = _trading_days_covered(trades)
    if len(days_covered) < MIN_TRADING_DAYS:
        return {"sufficient_data": False, "days_covered": len(days_covered), "needed": MIN_TRADING_DAYS}

    closed = [t for t in trades if t.get("net_pnl") not in (None, "", "None")]
    losses = [t for t in closed if float(t["net_pnl"]) < 0]

    loss_exit_reasons = {}
    for t in losses:
        r = t.get("exit_reason", "unknown")
        loss_exit_reasons[r] = loss_exit_reasons.get(r, 0) + 1

    block_counts = {}
    for a in audit:
        if a.get("passed") not in ("True", "true", True):
            reason = a.get("block_reason") or "unknown"
            block_counts[reason] = block_counts.get(reason, 0) + 1
    total_evaluated = len(audit) or 1
    block_pcts = {k: round(v / total_evaluated * 100, 1) for k, v in block_counts.items()}

    return {
        "sufficient_data": True,
        "days_covered": len(days_covered),
        "total_trades": len(closed),
        "total_losses": len(losses),
        "loss_exit_reasons": loss_exit_reasons,
        "block_reason_pcts": block_pcts,
    }


def generate_recommendations(analysis: dict, cfg) -> list:
    if not analysis.get("sufficient_data"):
        return []

    recs = []
    total_losses = analysis.get("total_losses", 0)
    loss_reasons = analysis.get("loss_exit_reasons", {})

    if total_losses >= 5:
        catastrophic_pct = loss_reasons.get("CATASTROPHIC", 0) / total_losses * 100
        if catastrophic_pct > 40:
            recs.append({
                "param": "reversal_confirm_polls",
                "current": cfg.reversal_confirm_polls,
                "suggested": max(1, cfg.reversal_confirm_polls - 1),
                "reason": (f"{catastrophic_pct:.0f}% of losses over the last {analysis['days_covered']} "
                          f"trading days exited via catastrophic backstop rather than OI-reversal - "
                          f"the reversal stop may be confirming too slowly to catch real reversals before "
                          f"the wide backstop has to step in. Suggest tightening reversal_confirm_polls."),
            })

    block_pcts = analysis.get("block_reason_pcts", {})
    if block_pcts:
        top_blocker = max(block_pcts, key=block_pcts.get)
        if block_pcts[top_blocker] > 50:
            recs.append({
                "param": None,   # informational only, no direct param change suggested
                "current": None, "suggested": None,
                "reason": (f"'{top_blocker}' is blocking {block_pcts[top_blocker]:.0f}% of all evaluated "
                          f"signals over the last {analysis['days_covered']} days - worth reviewing whether "
                          f"this filter's threshold is appropriately calibrated, or whether current market "
                          f"conditions are simply outside this strategy's intended regime."),
            })

    return recs


def format_telegram_message(analysis: dict, recs: list) -> str:
    if not analysis.get("sufficient_data"):
        return (f"Weekly analytics: not enough data yet ({analysis['days_covered']}/{analysis['needed']} "
                f"trading days). No recommendations until the minimum window is reached.")

    lines = [
        f"=== Weekly Analytics ({analysis['days_covered']} trading days) ===",
        f"Trades: {analysis['total_trades']}, Losses: {analysis['total_losses']}",
        f"Loss exit-reason breakdown: {analysis['loss_exit_reasons']}",
        f"Filter block %: {analysis['block_reason_pcts']}",
        "",
    ]
    if not recs:
        lines.append("No recommendations this week - nothing crossed a threshold worth flagging.")
    else:
        lines.append("Recommendations (reply APPLY <param> <value> to act on one, or ignore):")
        for r in recs:
            if r["param"]:
                lines.append(f"- {r['param']}: {r['current']} -> {r['suggested']}. {r['reason']}")
            else:
                lines.append(f"- {r['reason']}")
    return "\n".join(lines)


def apply_param_change(state, cfg, param: str, value, reason: str, source: str = "analytics_recommendation"):
    ok, error = validate(param, value)
    if not ok:
        return False, error

    old_value = getattr(cfg, param, None)
    setattr(cfg, param, value)
    save_override(param, value)
    state.log_param_change(param, old_value, value, reason, source)
    return True, f"{param} changed from {old_value} to {value}. Effective immediately and persisted across restarts."
