"""
Kotak Neo = execution-only (locked decision). Rebuilt against the actual
kotak-neo-python v3.0.x source (uploaded repo zip, confirmed field names -
not the announcement copy). This class must never be imported/instantiated
on the PAPER-mode code path - callers are responsible for that gating.

CONFIRMED AGAINST REAL SOURCE (not guessed):
- NeoAPI(...) constructor args must be passed as keywords - parameter order
  changed from v2 and 'environment' now defaults to 'prod' not 'uat'.
- totp_login()/totp_validate() unchanged in shape from v2.
- search_scrip(): expiry format is "DDMMMYYYY" e.g. "28JUN2023" (NOT the old
  v2 "%d-%b-%Y" format, NOT "YYYYMM" as one misleading internal docstring
  claims). Futures are searched via option_type="FUT" (SDK-only alias,
  maps to pOptionType=="XX" internally) - NOT option_type="".
- search_scrip() response rows use pSymbol (numeric instrument token, for
  quotes()) and pTrdSymbol (string, for place_order's trading_symbol) -
  two different fields for two different purposes, not one "instrument_token".
- quotes() response fields (confirmed via real sample in docs): "ltp"
  (string, needs float()), "open_int" for OI (NOT "oi"/"openInterest" as
  guessed against the old SDK).
- place_order() has NO algo_id parameter - stricter validation in this SDK
  version raises TypeError on unexpected kwargs. The SEBI Algo-ID appears
  to be tied to the consumer_key/app registration itself (via Kotak's algo
  desk), not passed per-order - confirm this with Kotak directly before
  going LIVE, since it changes what "registering the algo" actually means
  operationally.
- order_report() response fields: nOrdNo (order id), avgPrc, fldQty, ordSt
  (lowercase values e.g. "open"/"complete"/"rejected").
- Error handling stays dict-based ({"Error":...} / {"error":[...]}) for
  every REST method except totp_login/totp_validate, which can raise
  ApiException on a genuine network failure - unchanged from v2 in this
  respect, not a new try/except requirement across the board.
"""
import logging
from neo_api_client import NeoAPI

logger = logging.getLogger("kotak_client")

_ID_SEP = "|"   # composite id: "{pSymbol}|{pTrdSymbol}" - see module note on why


class KotakClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.sdk = NeoAPI(
            consumer_key=cfg.kotak_consumer_key,
            environment="prod",
            access_token=None,
            neo_fin_key=None,
        )
        self._logged_in = False

    def login(self, totp: str):
        result = self.sdk.totp_login(mobile_number=self.cfg.kotak_mobile, ucc=self.cfg.kotak_ucc, totp=totp)
        if "error" in result or "Error" in result:
            raise RuntimeError(f"Kotak totp_login failed: {result}")
        result = self.sdk.totp_validate(mpin=self.cfg.kotak_mpin)
        if "error" in result or "Error" in result:
            raise RuntimeError(f"Kotak totp_validate failed: {result}")
        self._logged_in = True
        logger.info("Kotak logged in (execution-only).")

    # ---------- instrument resolution ----------
    # Kotak's search_scrip() response needs two different fields for two
    # different purposes: pSymbol (numeric token, for quotes()) and
    # pTrdSymbol (string, for place_order's trading_symbol). Returned here
    # as a single delimited string so ResolvedInstrument.broker_ids stays a
    # uniform dict[str, str] across all three brokers - see
    # trading_symbol_from_id()/instrument_token_from_id() to unpack it.
    def resolve_futures(self, descriptor):
        expiry_str = descriptor.expiry.strftime("%d%b%Y").upper()
        result = self.sdk.search_scrip(
            exchange_segment="nse_fo", symbol=descriptor.underlying,
            expiry=expiry_str, option_type="FUT", strike_price="",
        )
        rows = result if isinstance(result, list) else []
        if not rows:
            logger.warning("Kotak: no futures match for %s expiry=%s", descriptor.underlying, expiry_str)
            return None
        row = rows[0]
        return f"{row.get('pSymbol')}{_ID_SEP}{row.get('pTrdSymbol')}"

    def resolve_option(self, descriptor):
        expiry_str = descriptor.expiry.strftime("%d%b%Y").upper()
        result = self.sdk.search_scrip(
            exchange_segment="nse_fo", symbol=descriptor.underlying,
            expiry=expiry_str, option_type=descriptor.instrument_type,
            strike_price=str(descriptor.strike),
        )
        rows = result if isinstance(result, list) else []
        if not rows:
            logger.warning("Kotak: no option match for %s %s %s expiry=%s",
                           descriptor.underlying, descriptor.strike, descriptor.instrument_type, expiry_str)
            return None
        row = rows[0]
        return f"{row.get('pSymbol')}{_ID_SEP}{row.get('pTrdSymbol')}"

    @staticmethod
    def instrument_token_from_id(composite_id: str):
        if not composite_id:
            return None
        return composite_id.split(_ID_SEP)[0]

    @staticmethod
    def trading_symbol_from_id(composite_id: str):
        if not composite_id or _ID_SEP not in composite_id:
            return None
        return composite_id.split(_ID_SEP, 1)[1]

    def get_ltp(self, composite_id: str):
        """Used only by the narrow emergency-read fallback (both data feeds
        down with a position open) and by the instrument-resolution sanity
        check - never part of the normal signal pipeline, per locked design."""
        instrument_token = self.instrument_token_from_id(composite_id)
        if not instrument_token:
            return None
        try:
            rows = self.sdk.quotes(
                instrument_tokens=[{"instrument_token": instrument_token, "exchange_segment": "nse_fo"}],
                quote_type="ltp",
            )
            if isinstance(rows, list) and rows:
                ltp = rows[0].get("ltp")
                return float(ltp) if ltp is not None else None
        except Exception as exc:
            logger.warning("Kotak emergency LTP fetch failed for %s: %s", composite_id, exc)
        return None

    def get_pre_trade_quote(self, composite_id: str):
        """Second narrow exception to Kotak's execution-only role, same
        justification as get_ltp's emergency fallback: this is fundamentally
        an "is it safe to execute this order right now" check, not a data-
        feed responsibility. Used only in the moment immediately before
        firing a LIVE order, never part of the polling signal pipeline.
        Returns {"ltp": float, "low_circuit": float, "high_circuit": float}
        or None on failure - all confirmed real fields from a live sample
        response (quote_type='all' includes circuit band as
        low_price_range/high_price_range)."""
        instrument_token = self.instrument_token_from_id(composite_id)
        if not instrument_token:
            return None
        try:
            rows = self.sdk.quotes(
                instrument_tokens=[{"instrument_token": instrument_token, "exchange_segment": "nse_fo"}],
                quote_type="all",
            )
            if not isinstance(rows, list) or not rows:
                return None
            row = rows[0]
            return {
                "ltp": float(row.get("ltp", 0)) or None,
                "low_circuit": float(row.get("low_price_range", 0)) or None,
                "high_circuit": float(row.get("high_price_range", 0)) or None,
            }
        except Exception as exc:
            logger.warning("Kotak pre-trade quote fetch failed for %s: %s", composite_id, exc)
            return None

    def check_margin(self, composite_id: str, quantity: int, price: str, transaction_type: str = "B"):
        """Pre-flight margin check before placing a LIVE order - confirmed
        real response fields: insufFund (shortfall, "0.000000" = sufficient),
        ordMrgn (margin this order needs), rmsVldtd ("OK" if the risk
        system validated it). Returns (sufficient: bool, detail: dict)."""
        instrument_token = self.instrument_token_from_id(composite_id)
        if not instrument_token:
            return False, {"error": "no instrument token resolved"}
        try:
            resp = self.sdk.margin_required(
                exchange_segment="nse_fo", price=price, order_type="MKT", product="MIS",
                quantity=str(quantity), instrument_token=instrument_token, transaction_type=transaction_type,
            )
            data = resp.get("data", {}) if isinstance(resp, dict) else {}
            insuf = float(data.get("insufFund", 0) or 0)
            rms_ok = data.get("rmsVldtd", "").upper() == "OK"
            sufficient = insuf <= 0 and rms_ok
            return sufficient, data
        except Exception as exc:
            logger.warning("Kotak margin check failed for %s: %s", composite_id, exc)
            return False, {"error": str(exc)}

    # ---------- order lifecycle ----------
    def place_order(self, trading_symbol, exchange_segment, transaction_type, quantity):
        """No algo_id parameter - see module docstring. SEBI Algo-ID
        compliance is handled at the consumer_key/app-registration level,
        not per-order, per the confirmed real SDK signature."""
        return self.sdk.place_order(
            exchange_segment=exchange_segment, product="MIS", price="0", order_type="MKT",
            quantity=str(quantity), validity="DAY", trading_symbol=trading_symbol,
            transaction_type=transaction_type, amo="NO",
        )

    def order_status(self, order_id: str):
        return self.sdk.order_report(order_id=order_id)

    def positions(self):
        return self.sdk.positions()   # confirmed: no parameters in this SDK version

    def logout(self):
        if self._logged_in:
            self.sdk.logout()
            self._logged_in = False
