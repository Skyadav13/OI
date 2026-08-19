"""
Total dual-feed outage handling (locked design): if BOTH Sharekhan and
IIFL are stale simultaneously while a position is open, Kotak temporarily
supplies LTP-only via its emergency-read path. In this mode: only the
catastrophic price backstop remains active (OI-reversal/trailing need OI
data neither dead feed nor Kotak reliably provides), no new entries, one
alert on entry into the state (not spammed every poll), reverts the
instant either data feed's heartbeat recovers.
"""
import logging

logger = logging.getLogger("degraded_mode")


class DegradedModeController:
    def __init__(self, telegram):
        self.telegram = telegram
        self.active = False

    def evaluate(self, health, has_open_position: bool) -> bool:
        both_down = health.is_stale("sharekhan") and health.is_stale("iifl")
        should_be_active = both_down and has_open_position

        if should_be_active and not self.active:
            self.active = True
            self.telegram.send(
                "⚠️ EMERGENCY: both Sharekhan and IIFL feeds are down with a position open. "
                "Switching to Kotak emergency-read mode: catastrophic backstop only, "
                "OI-reversal/trailing exits disabled, no new entries until a data feed recovers."
            )
            logger.error("Entered degraded mode - both data feeds stale, position open.")

        elif not should_be_active and self.active:
            self.active = False
            self.telegram.send("Data feed recovered - resuming normal OI-based exit monitoring.")
            logger.info("Exited degraded mode.")

        return self.active

    def get_emergency_ltp(self, kotak_client, kotak_instrument_id):
        if kotak_client is None or kotak_instrument_id is None:
            return None
        return kotak_client.get_ltp(kotak_instrument_id)
