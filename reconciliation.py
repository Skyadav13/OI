"""
Startup reconciliation (locked design): never trust the local state file
blindly - compare against Kotak's actual live positions before resuming
anything. PAPER mode has nothing external to reconcile against (Kotak is
absent entirely) and just trusts the atomically-written state file.

KNOWN LIMITATION, stated plainly rather than hidden: if a resumed LIVE
position's OI-reversal baseline depended on classification_history built
up before the crash, that history is gone - engine.classification_history
starts empty on restart. A resumed position's reversal check effectively
restarts its confirmation window from the moment of restart, not from the
original fill time. This is a known, bounded cost (per the original
rebase-vs-offset reasoning) - the alternative (fabricating history) risks
silently wrong direction reads, which is worse.
"""
import logging

logger = logging.getLogger("reconciliation")


def reconcile_on_startup(mode: str, state, kotak_client, telegram):
    if mode != "LIVE" or kotak_client is None:
        logger.info("PAPER mode or no Kotak client - trusting local state file, no external reconciliation needed.")
        return

    state_position = state.get("open_position")

    try:
        live_positions = kotak_client.positions()
    except Exception as exc:
        telegram.send(f"⚠️ Could not fetch Kotak positions for startup reconciliation: {exc}. "
                      "Proceeding with extreme caution - verify manually before trusting the bot's state.")
        logger.error("Reconciliation: Kotak positions() call failed: %s", exc)
        return

    kotak_has_open = _has_nonzero_position(live_positions)

    if state_position is None and not kotak_has_open:
        logger.info("Reconciliation: no position expected, none found on Kotak. Clean start.")
        return

    if state_position is not None and not kotak_has_open:
        logger.warning("Reconciliation: local state showed an open position but Kotak has none - "
                       "position was closed while bot was down (manually or by Kotak's own risk system).")
        telegram.send("⚠️ Reconciliation: a position the bot thought was open is not open on Kotak. "
                      "Marking closed locally. If this is unexpected, check your Kotak account directly.")
        state.set("open_position", None)
        return

    if state_position is None and kotak_has_open:
        logger.error("Reconciliation: Kotak shows an open position the bot has no record of - "
                     "UNMANAGED POSITION.")
        telegram.send("🚨 URGENT: Kotak shows an open position this bot has no record of. "
                      "This is unmanaged - the bot cannot safely resume normal monitoring on it. "
                      "Check your Kotak account and confirm manually before trusting the bot's next actions. "
                      "The bot will apply catastrophic-backstop-only monitoring to any position it can "
                      "identify, but will not resume full OI-based management without your confirmation.")
        state.set("unmanaged_position_alert", True)
        return

    logger.info("Reconciliation: local state and Kotak both show an open position - resuming monitoring. "
               "Note: OI-reversal confirmation window restarts fresh from this point (see module docstring).")
    telegram.send("Reconciliation OK: resuming monitoring of the open position found on both local state "
                  "and Kotak. Note: OI-reversal exit logic restarts its confirmation window from now, "
                  "since classification history doesn't survive a restart.")


def _has_nonzero_position(live_positions) -> bool:
    if not live_positions:
        return False
    rows = live_positions if isinstance(live_positions, list) else live_positions.get("data", [])
    for row in rows:
        qty = row.get("net_quantity") or row.get("netQty") or row.get("quantity") or 0
        try:
            if int(qty) != 0:
                return True
        except (TypeError, ValueError):
            continue
    return False
