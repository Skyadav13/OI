"""
Sharekhan = primary data feed (locked decision). Login reuses the two-tier
pattern from your uploaded sharekhan_telegram_bot.py: reverse-engineered
auto-login first, official SDK login_url() as the alert-and-wait fallback.
CONFIRMED against the real official source (github.com/Sharekhan-API/
shareconnectpython, uploaded repo zip - not just the README): the official
SDK has no automated headless login at all. Real flow is browser-based
login_url() -> human pastes back a request_token -> generate_session()
(pure local AES-GCM crypto, no network) -> get_access_token() (the actual
network call). generate_session()/get_access_token()'s signatures matched
what was already here - no change needed to the login chain itself.

ARCHITECTURE CORRECTION (still holds): the official SDK has no REST
quote/LTP/OI endpoint. Live data only via SharekhanWebSocket. The only REST
market-data call is historicaldata(exchange, scripcode, interval).

CONFIRMED BUGS FIXED against real source:
- Exchange code for NSE F&O is "NF", not "NFO" (an incorrectly-carried-over
  Kotak convention) - confirmed via the official websocket example's token
  prefixes ("NF37833" etc.).
- Subscription protocol is two-step, not the single subscribe() call this
  previously used: a generic subscribe() channel-open, then a SEPARATE
  fetchData() call with action="feed" where value is ONE comma-joined
  string of exchange-prefixed tokens (e.g. "NF12345,NF67890"), not a list
  of separate unprefixed tokens.
- Expiry format "%d/%m/%Y" was already correct - independently confirmed
  by a real placeOrder() sample showing "31/03/2023".

GENUINELY UNRESOLVABLE FROM SOURCE (not a guess to fix, a real gap):
the SDK's own _parse_binary_data() is an unimplemented stub (returns None)
for binary websocket frames. Whether real NIFTY F&O tick messages arrive
as JSON/text or raw binary cannot be determined from the public repo -
_handle_tick() now logs the first raw message it receives so the first
live connection serves as the actual verification step. If it turns out
binary with no usable content, Sharekhan's tick protocol spec isn't
published publicly - would need requesting from their support directly.

STILL UNVERIFIED (genuine field-name guesses, lower severity):
master()'s response row field names - no sample response appears anywhere
in the public SDK's source, docs, or examples.
"""
import re
import logging
import threading
import requests

from SharekhanApi.sharekhanConnect import SharekhanConnect
from SharekhanApi.sharekhanWebsocket import SharekhanWebSocket

logger = logging.getLogger("sharekhan_client")


class SharekhanClient:
    AUTH_URL = "https://api.sharekhan.com/skapi/auth/login"
    TWOFA_URL = "https://api.sharekhan.com/skapi/auth/verifyOTP"

    def __init__(self, cfg):
        self.cfg = cfg
        self.sdk = SharekhanConnect(cfg.sharekhan_api_key)
        self.access_token = None
        self._master_cache = None
        self._logged_first_tick = False
        self._ws = None
        self._tick_cache = {}       # {scripCode: {"ltp": float, "oi": float, "last_update": float}}
        self._tick_lock = threading.Lock()
        self._subscribed_tokens = set()

    # ---------- auth (two-tier, per locked pattern) ----------
    def login(self) -> bool:
        try:
            self._auto_login()
            self._start_websocket()
            return True
        except Exception as exc:
            fallback_url = self.sdk.login_url(vendor_key="", version_id="1005")
            logger.error("Sharekhan auto-login failed: %s. Manual fallback: %s", exc, fallback_url)
            return False

    def _auto_login(self):
        import pyotp
        session = requests.session()
        session.headers.update({"X-Requested-With": "XMLHttpRequest", "Origin": "https://api.sharekhan.com"})

        auth_payload = {
            "loginId": self.cfg.sharekhan_login_id, "membershipPwd": self.cfg.sharekhan_password,
            "apiKey": self.cfg.sharekhan_api_key, "vendorKey": "", "userId": "",
            "versionId": "1005", "state": "botlogin", "isLoginAfterActivation": "0",
        }
        resp = session.post(self.AUTH_URL, json=auth_payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("statusCode") != 100:
            raise RuntimeError(f"Sharekhan login step failed: {data}")

        totp_code = pyotp.TOTP(self.cfg.sharekhan_totp_secret).now().zfill(6)
        twofa_payload = {
            "loginId": self.cfg.sharekhan_login_id, "apiKey": self.cfg.sharekhan_api_key,
            "vendorKey": "", "userId": "", "versionId": "1005", "state": "botlogin",
            "otp": "", "totp": totp_code, "validateBy": "TOTP",
            "requestSessionId": data["requestSessionId"],
        }
        resp = session.post(self.TWOFA_URL, json=twofa_payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("statusCode") != 200 or "responseUrl" not in data:
            raise RuntimeError(f"Sharekhan TOTP verification failed: {data}")

        match = re.search(r"request_token=([^&]+)", data["responseUrl"])
        if not match:
            raise RuntimeError(f"No request_token in Sharekhan response URL: {data['responseUrl']}")
        request_token = match.group(1)

        # Confirmed signatures from official README:
        #   generate_session(request_token, secret_key)
        #   get_access_token(api_key, session, state, versionId=version_id)
        sess = self.sdk.generate_session(request_token, self.cfg.sharekhan_secret_key)
        resp = self.sdk.get_access_token(self.cfg.sharekhan_api_key, sess, "botlogin", versionId="1005")
        data = resp.get("data", resp) if isinstance(resp, dict) and "data" in resp else resp
        token = (data.get("token") or data.get("accessToken")) if isinstance(data, dict) else None
        if not token:
            raise RuntimeError(f"No access token in Sharekhan response: {resp}")

        self.access_token = token
        self.sdk = SharekhanConnect(api_key=self.cfg.sharekhan_api_key, access_token=token)
        logger.info("Sharekhan logged in.")

    # ---------- live data via websocket ----------
    def _start_websocket(self):
        self._ws = SharekhanWebSocket(self.access_token)

        def on_data(wsapp, message):
            self._handle_tick(message)

        def on_open(wsapp):
            logger.info("Sharekhan websocket connected.")
            if self._subscribed_tokens:
                self._send_subscription()

        def on_error(wsapp, error):
            logger.warning("Sharekhan websocket error: %s", error)

        def on_close(wsapp):
            logger.warning("Sharekhan websocket closed - self-heal/reconnect handled by main loop's heartbeat check.")

        self._ws.on_open = on_open
        self._ws.on_data = on_data
        self._ws.on_error = on_error
        self._ws.on_close = on_close

        threading.Thread(target=self._ws.connect, daemon=True).start()

    def _handle_tick(self, message):
        """CONFIRMED FROM REAL SOURCE (not guessed): the SDK's own
        _parse_binary_data() is an unimplemented stub (returns None) for
        data_type==2 (binary) websocket frames - there is no shipped
        decoder for that case anywhere in the public SDK. Whether real
        NIFTY F&O feed messages arrive as binary or as plain text/JSON
        cannot be determined from source code alone - this is a genuine
        unresolvable-without-a-live-connection gap, not a guessed field
        name. On first live connection, this logs the raw message
        type/repr at INFO level specifically so that moment serves as the
        actual verification step. If messages turn out to be binary with
        no usable content (parsed as None), Sharekhan's proprietary tick
        protocol spec would need requesting directly from their support -
        it is not published in this SDK's public repo."""
        try:
            import json
            if message is None:
                logger.warning("Sharekhan tick arrived as None - if this repeats, real feed messages are "
                               "likely binary-encoded and this SDK ships no decoder for that case. "
                               "Contact Sharekhan support for their binary tick protocol spec.")
                return
            if not self._logged_first_tick:
                logger.info("First Sharekhan tick received - raw type=%s repr=%.200r", type(message), message)
                self._logged_first_tick = True

            data = json.loads(message) if isinstance(message, str) else message
            if not isinstance(data, dict):
                logger.debug("Sharekhan tick was not a dict after parsing (type=%s) - skipping.", type(data))
                return

            # VERIFY: field names below are best-effort guesses consistent
            # with scripCode being the confirmed instrument-id field name
            # used elsewhere in the SDK (order params, master() lookups) -
            # not confirmed against an actual live tick payload.
            token = str(data.get("scripCode") or data.get("token") or "")
            if not token:
                return
            with self._tick_lock:
                import time
                entry = self._tick_cache.setdefault(token, {})
                if "ltp" in data or "lastTradedPrice" in data:
                    entry["ltp"] = float(data.get("ltp") or data.get("lastTradedPrice"))
                if "oi" in data or "openInterest" in data:
                    entry["oi"] = float(data.get("oi") or data.get("openInterest"))
                entry["last_update"] = time.time()
        except Exception as exc:
            logger.debug("Sharekhan tick parse issue (non-fatal): %s", exc)

    def subscribe(self, scrip_codes: list, exchange_prefix: str = "NF"):
        """exchange_prefix confirmed from the official websocket example
        (token format "NF37833" for NSE F&O, "NC..." for NSE Cash,
        "MX..." for MCX, "RN..." for currency) - "NF" is correct for
        NIFTY futures/options specifically."""
        self._subscribed_tokens.update(f"{exchange_prefix}{c}" for c in scrip_codes)
        if self._ws:
            self._send_subscription()

    def _send_subscription(self):
        """CONFIRMED FROM REAL SOURCE (not guessed): the official example
        shows a two-step protocol - a generic channel-open subscribe(),
        then a separate fetchData() call with action='feed' where value is
        a SINGLE comma-joined string of exchange-prefixed tokens, not a
        list of separate token strings. This replaces an earlier version
        that called subscribe() alone with an unprefixed token list, which
        does not match the real protocol."""
        try:
            self._ws.subscribe({"action": "subscribe", "key": ["feed"], "value": [""]})
            token_string = ",".join(sorted(self._subscribed_tokens))
            self._ws.fetchData({"action": "feed", "key": ["ltp"], "value": [token_string]})
        except Exception as exc:
            logger.warning("Sharekhan subscribe/fetchData call failed: %s", exc)

    # ---------- instrument resolution ----------
    def _load_master(self, exchange: str = "NF"):
        """CONFIRMED FROM REAL SOURCE: method name master(exchange) is
        correct. Exchange code fixed from an earlier "NFO" (a Kotak
        convention incorrectly carried over) to "NF" - confirmed correct
        for NSE F&O via the websocket example's token prefix convention
        ("NF37833" etc.). Response row field names still NOT confirmed -
        no master() sample response appears anywhere in the public SDK's
        source, docs, or examples - adjust the .get() keys below once
        verified against a real response."""
        if self._master_cache is None:
            self._master_cache = self.sdk.master(exchange)
        return self._master_cache

    def resolve_futures(self, descriptor):
        master = self._load_master()
        rows = master if isinstance(master, list) else master.get("data", [])
        for row in rows:
            if (str(row.get("tradingSymbol", "")).startswith(descriptor.underlying) and
                    row.get("instrumentType") in ("FUTIDX", "FI") and
                    row.get("expiry") == descriptor.expiry.strftime("%d/%m/%Y")):
                return str(row.get("scripCode"))
        return None

    def resolve_option(self, descriptor):
        master = self._load_master()
        rows = master if isinstance(master, list) else master.get("data", [])
        for row in rows:
            if (str(row.get("tradingSymbol", "")).startswith(descriptor.underlying) and
                    row.get("optionType") == descriptor.instrument_type and
                    row.get("strikePrice") == descriptor.strike and
                    row.get("expiry") == descriptor.expiry.strftime("%d/%m/%Y")):
                return str(row.get("scripCode"))
        return None

    # ---------- market data (from websocket cache, not REST) ----------
    def get_ltp(self, scrip_code: str):
        with self._tick_lock:
            return self._tick_cache.get(str(scrip_code), {}).get("ltp")

    def get_oi(self, scrip_code: str):
        with self._tick_lock:
            return self._tick_cache.get(str(scrip_code), {}).get("oi")

    def last_tick_age_seconds(self, scrip_code: str):
        import time
        with self._tick_lock:
            ts = self._tick_cache.get(str(scrip_code), {}).get("last_update")
        return (time.time() - ts) if ts else None

    def historicaldata(self, exchange: str, scrip_code, interval: str):
        """Confirmed signature: historicaldata(exchange, scripcode, interval)
        - no from/to date params shown in the README, unlike IIFL's version."""
        return self.sdk.historicaldata(exchange, scrip_code, interval)
