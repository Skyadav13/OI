"""
Instruments are described broker-agnostically (underlying/type/expiry/strike)
and resolved independently against each broker's own contract master, per
the locked design. Downstream code (signal engine, trade manager) should
only ever reason about Descriptors - never a raw broker ID directly.

Broker client objects (Phase 2) are expected to implement:
    resolve_futures(descriptor) -> broker_instrument_id (str) or None
    resolve_option(descriptor)  -> broker_instrument_id (str) or None
    get_ltp(broker_instrument_id) -> float or None   (used for sanity check)
"""
import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

logger = logging.getLogger("instruments")


@dataclass(frozen=True)
class Descriptor:
    underlying: str
    instrument_type: str   # "FUT" | "CE" | "PE"
    expiry: date
    strike: Optional[int] = None   # None for futures

    def __str__(self):
        if self.instrument_type == "FUT":
            return f"{self.underlying} FUT {self.expiry.isoformat()}"
        return f"{self.underlying} {self.strike} {self.instrument_type} {self.expiry.isoformat()}"


class ResolvedInstrument:
    """Holds the same logical instrument's ID across all three brokers."""
    def __init__(self, descriptor: Descriptor):
        self.descriptor = descriptor
        self.broker_ids: dict[str, str] = {}   # e.g. {"sharekhan": "...", "iifl": "...", "kotak": "..."}

    def has(self, broker: str) -> bool:
        return broker in self.broker_ids and self.broker_ids[broker] is not None


class InstrumentResolver:
    SANITY_CHECK_MAX_DIVERGENCE_PCT = 5.0   # cross-broker LTP shouldn't differ more than this at resolution time

    def __init__(self, brokers: dict):
        """brokers: {"sharekhan": client, "iifl": client, "kotak": client_or_None}
        Kotak may be None entirely in PAPER mode - resolver must not require it."""
        self.brokers = brokers

    def resolve(self, descriptor: Descriptor) -> ResolvedInstrument:
        resolved = ResolvedInstrument(descriptor)

        for broker_name, client in self.brokers.items():
            if client is None:
                continue
            try:
                if descriptor.instrument_type == "FUT":
                    broker_id = client.resolve_futures(descriptor)
                else:
                    broker_id = client.resolve_option(descriptor)
                resolved.broker_ids[broker_name] = broker_id
                if broker_id is None:
                    logger.warning("%s: could not resolve %s", broker_name, descriptor)
            except Exception as exc:
                logger.error("%s: resolution error for %s: %s", broker_name, descriptor, exc)
                resolved.broker_ids[broker_name] = None

        self._sanity_check(resolved)
        return resolved

    def _sanity_check(self, resolved: ResolvedInstrument):
        """One-time cross-broker LTP check after resolution - catches a
        mis-resolved instrument immediately rather than silently trading
        the wrong contract, per the locked design."""
        ltps = {}
        for broker_name, broker_id in resolved.broker_ids.items():
            if broker_id is None:
                continue
            client = self.brokers.get(broker_name)
            if client is None:
                continue
            try:
                ltp = client.get_ltp(broker_id)
                if ltp:
                    ltps[broker_name] = ltp
            except Exception as exc:
                logger.warning("%s: LTP sanity check failed for %s: %s", broker_name, resolved.descriptor, exc)

        if len(ltps) < 2:
            return  # nothing to cross-check yet

        values = list(ltps.values())
        spread_pct = (max(values) - min(values)) / min(values) * 100 if min(values) else 0
        if spread_pct > self.SANITY_CHECK_MAX_DIVERGENCE_PCT:
            logger.error(
                "Instrument resolution sanity check FAILED for %s: broker LTPs disagree by %.1f%% (%s). "
                "Possible mis-resolved contract - do not trade this instrument until manually verified.",
                resolved.descriptor, spread_pct, ltps,
            )
