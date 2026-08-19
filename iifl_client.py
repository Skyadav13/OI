"""
IIFL = secondary/cross-check data feed (locked decision). Login reuses the
two-tier pattern: reverse-engineered SSO auto-login (from your uploaded
iifl_auto_login.py) as primary, official browser SSO link as fallback.

Request shapes below are CONFIRMED against IIFL's official Postman
collection (RELEASE-V1-IIFL-OPEN-API-COLLECTIONS). Response field names
are NOT confirmed - the collection has no saved example responses.
VERIFY _parse_quote_row()/_parse_oi_row() against a real live response.
"""
import logging
import requests

from iifl_crypto import generate_keypair, client_side_encrypt, client_side_decrypt

logger = logging.getLogger("iifl_client")

API_BASE = "https://api.iiflcapital.com/v1"
IDAAS_BASE = "https://idaas.iiflsecurities.com/v2/access"


class IIFLClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.session = requests.Session()
        self.trading_token = None
        self._client_pub_b64 = None
        self._client_priv_obj = None
        self._server_pub_b64 = None
        self._contract_master_cache = None

    @property
    def login_url(self) -> str:
        return f"https://markets.iiflcapital.com/?appkey={self.cfg.iifl_app_key}&v=1"

    def login(self) -> bool:
        try:
            self._auto_login()
            return True
        except Exception as exc:
            logger.error("IIFL auto-login failed: %s. Manual fallback: %s", exc, self.login_url)
            return False

    def _headers(self, extra=None):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/plain, */*"}
        if extra:
            h.update(extra)
        return h

    def _auto_login(self):
        # Step 1: vendor details
        resp = self.session.post(f"{API_BASE}/sso/vendor/details", headers=self._headers(),
                                  json={"vendor": self.cfg.iifl_app_key}, timeout=15)
        resp.raise_for_status()

        # Step 2-3: key exchange
        self._client_pub_b64, _, self._client_priv_obj = generate_keypair()
        resp = self.session.post(f"{IDAAS_BASE}/get/encKey", headers=self._headers(),
                                  json={"ceData": self._client_pub_b64}, timeout=15)
        resp.raise_for_status()
        self._server_pub_b64 = resp.json().get("cPubKey")
        if not self._server_pub_b64:
            raise RuntimeError("IIFL key exchange failed")

        # Step 4: password
        from totp import generate_totp
        payload = {"userId": self.cfg.iifl_client_code, "password": self.cfg.iifl_password,
                   "deviceId": "bot-instance", "versionNo": "A.0.0.1",
                   "appName": "IIFLMARKETSWEB", "osName": "WEBINTERNAL"}
        enc = client_side_encrypt(payload, self._server_pub_b64, self._client_pub_b64)
        resp = self.session.post(f"{IDAAS_BASE}/pwd/validate", headers=self._headers(),
                                  json={"cEncData": enc}, timeout=15)
        data = self._decrypt(resp)
        interim_token = (data.get("result") or {}).get("token", "")

        # Step 5: TOTP
        code = generate_totp(self.cfg.iifl_totp_secret)
        payload = {"userId": self.cfg.iifl_client_code, "totp": code,
                   "deviceId": "bot-instance", "appName": "IIFLMARKETSWEB", "osName": "WEBINTERNAL"}
        enc = client_side_encrypt(payload, self._server_pub_b64, self._client_pub_b64)
        resp = self.session.post(f"{IDAAS_BASE}/topt/verify", headers=self._headers(
            {"Authorization": f"Bearer {interim_token}"}), json={"cEncData": enc}, timeout=15)
        data = self._decrypt(resp)
        access_token = (data.get("result") or {}).get("accessToken", "")
        if not access_token:
            raise RuntimeError(f"IIFL TOTP verify did not return a token: {data}")

        # Step 6: authorize
        resp = self.session.post(f"{API_BASE}/sso/vendor/authorize",
                                  headers=self._headers({"Authorization": f"Bearer {access_token}"}),
                                  json={"userId": self.cfg.iifl_client_code, "vendor": self.cfg.iifl_app_key},
                                  timeout=15)
        resp.raise_for_status()
        raw = resp.json()
        auth_code = raw.get("authCode") or (raw.get("result") or {}).get("authCode")
        if not auth_code:
            raise RuntimeError(f"Could not find IIFL authCode: {raw}")

        # Step 7: trading session
        import hashlib
        checksum = hashlib.sha256(
            f"{self.cfg.iifl_client_code}{auth_code}{self.cfg.iifl_app_secret}".encode()
        ).hexdigest()
        resp = self.session.post(f"{API_BASE}/getusersession", headers=self._headers(),
                                  json={"checkSum": checksum}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        token = data.get("userSession") or (data.get("result") or {}).get("token")
        if not token:
            raise RuntimeError(f"IIFL getusersession did not return a token: {data}")

        self.trading_token = token
        logger.info("IIFL logged in.")

    def _decrypt(self, resp):
        resp.raise_for_status()
        raw = resp.json()
        if "cRespEncData" not in raw:
            return raw
        return client_side_decrypt(raw["cRespEncData"], self._client_priv_obj)

    # ---------- instrument resolution ----------
    def _load_contract_master(self):
        if self._contract_master_cache is None:
            resp = requests.get(f"{API_BASE}/contractfiles/NSEFO.json", timeout=30)
            resp.raise_for_status()
            self._contract_master_cache = resp.json()
        return self._contract_master_cache

    def resolve_futures(self, descriptor):
        master = self._load_contract_master()
        for row in master if isinstance(master, list) else master.get("data", []):
            if (row.get("Symbol", "").startswith(descriptor.underlying) and
                    row.get("InstrumentType") in ("FUTIDX", "FUT") and
                    row.get("Expiry") == descriptor.expiry.isoformat()):
                return str(row.get("InstrumentId") or row.get("instrumentId"))
        return None

    def resolve_option(self, descriptor):
        master = self._load_contract_master()
        for row in master if isinstance(master, list) else master.get("data", []):
            if (row.get("Symbol", "").startswith(descriptor.underlying) and
                    row.get("OptionType") == descriptor.instrument_type and
                    row.get("StrikePrice") == descriptor.strike and
                    row.get("Expiry") == descriptor.expiry.isoformat()):
                return str(row.get("InstrumentId") or row.get("instrumentId"))
        return None

    def get_nifty_futures_expiries(self, underlying: str = "NIFTY"):
        """Dynamic expiry detection (locked design requirement - no
        hardcoded weekday/holiday assumptions). Pulls distinct NIFTY
        futures expiry dates directly from IIFL's live contract master,
        sorted ascending. Index 0 = current front-week expiry, index 1 =
        next-week expiry."""
        import datetime
        master = self._load_contract_master()
        rows = master if isinstance(master, list) else master.get("data", [])
        expiries = set()
        for row in rows:
            if (row.get("Symbol", "").startswith(underlying) and
                    row.get("InstrumentType") in ("FUTIDX", "FUT")):
                exp = row.get("Expiry")
                if exp:
                    try:
                        expiries.add(datetime.date.fromisoformat(exp))
                    except ValueError:
                        continue
        return sorted(expiries)

    # ---------- market data (request shapes confirmed, response fields NOT) ----------
    def get_ltp(self, instrument_id: str):
        row = self._market_quote_raw(instrument_id)
        return self._parse_quote_row(row).get("ltp") if row else None

    def get_oi(self, instrument_id: str):
        try:
            resp = self.session.post(
                f"{API_BASE}/marketdata/openinterest",
                headers=self._headers({"Authorization": f"Bearer {self.trading_token}"}),
                json={"exchange": "NSEFO", "instrumentId": instrument_id}, timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            # VERIFY exact field name once a live response is seen
            result = data.get("result") or data
            oi = result.get("openInterest") or result.get("oi")
            return float(oi) if oi is not None else None
        except Exception as exc:
            logger.warning("IIFL OI fetch failed for %s: %s", instrument_id, exc)
            return None

    def _market_quote_raw(self, instrument_id: str):
        try:
            resp = self.session.post(
                f"{API_BASE}/marketdata/marketquotes",
                headers=self._headers({"Authorization": f"Bearer {self.trading_token}"}),
                json=[{"exchange": "NSEFO", "instrumentId": instrument_id}], timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            rows = data.get("result") or data
            return rows[0] if isinstance(rows, list) and rows else None
        except Exception as exc:
            logger.warning("IIFL quote fetch failed for %s: %s", instrument_id, exc)
            return None

    @staticmethod
    def _parse_quote_row(row: dict) -> dict:
        if not row:
            return {}
        ltp = row.get("ltp") or row.get("lastTradedPrice")
        return {"ltp": float(ltp) if ltp is not None else None}

    def historicaldata(self, instrument_id: str, interval: str, from_date: str, to_date: str):
        resp = self.session.post(
            f"{API_BASE}/marketdata/historicaldata",
            headers=self._headers({"Authorization": f"Bearer {self.trading_token}"}),
            json={"exchange": "NSEFO", "instrumentId": instrument_id, "interval": interval,
                  "fromDate": from_date, "toDate": to_date}, timeout=20,
        )
        resp.raise_for_status()
        return resp.json()
