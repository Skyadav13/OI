"""
One-shot diagnostic: logs into Sharekhan + IIFL, resolves NIFTY's current
futures contract, requests a live quote/tick from each, prints the RAW
response, then exits. Not part of the bot's runtime - this exists purely
to close the two remaining VERIFY gaps (Sharekhan's tick wire format,
IIFL's response field names) without needing a full trading session.

Deliberately avoids printing anything token/credential-shaped - only
market data responses are logged. Review output before pasting it
anywhere, regardless.

Usage:
    python3 diagnostic_probe.py
Exits after WAIT_SECONDS or as soon as both a real Sharekhan tick and an
IIFL quote have been captured, whichever comes first.
"""
import sys
import time

from config import Config
from sharekhan_client import SharekhanClient
from iifl_client import IIFLClient
from expiry_utils import get_calc_and_trade_expiry
from instruments import Descriptor

WAIT_SECONDS = 120


def main():
    cfg = Config()
    results = {"sharekhan_tick": None, "iifl_quote": None, "iifl_oi": None}
    sk_id, iifl_id = None, None

    print("\n=== Sharekhan login ===")
    sharekhan = SharekhanClient(cfg)
    sharekhan_ok = sharekhan.login()
    print(f"Sharekhan login: {'OK' if sharekhan_ok else 'FAILED - check credentials/2FA'}")

    print("\n=== IIFL login ===")
    iifl = IIFLClient(cfg)
    iifl_ok = iifl.login()
    print(f"IIFL login: {'OK' if iifl_ok else 'FAILED - check credentials/2FA'}")

    if not iifl_ok:
        print("\nCannot determine expiry without IIFL's contract master - aborting probe.")
        sys.exit(1)

    try:
        expiries = iifl.get_nifty_futures_expiries(cfg.underlying_symbol)
        calc_expiry, _, _ = get_calc_and_trade_expiry(expiries)
    except Exception as exc:
        print(f"Could not determine expiry: {exc}")
        sys.exit(1)

    print(f"\nUsing front-week expiry: {calc_expiry}")
    descriptor = Descriptor(cfg.underlying_symbol, "FUT", calc_expiry)

    if sharekhan_ok:
        try:
            sk_id = sharekhan.resolve_futures(descriptor)
            print(f"\nSharekhan resolved scripCode: {sk_id}")
            if sk_id:
                sharekhan.subscribe([sk_id])
        except Exception as exc:
            print(f"Sharekhan resolve_futures failed: {exc}")

    try:
        iifl_id = iifl.resolve_futures(descriptor)
        print(f"IIFL resolved instrumentId: {iifl_id}")
    except Exception as exc:
        print(f"IIFL resolve_futures failed: {exc}")

    print(f"\nWaiting up to {WAIT_SECONDS}s for a real Sharekhan tick and IIFL quote...\n")
    deadline = time.time() + WAIT_SECONDS

    while time.time() < deadline:
        if results["sharekhan_tick"] is None and sk_id:
            with sharekhan._tick_lock:
                cached = sharekhan._tick_cache.get(sk_id)
            if cached:
                results["sharekhan_tick"] = cached
                print(f"\n>>> RAW Sharekhan cached tick (post-parse): {cached}")

        if results["iifl_quote"] is None and iifl_id:
            try:
                row = iifl._market_quote_raw(iifl_id)
                if row:
                    results["iifl_quote"] = row
                    print(f"\n>>> RAW IIFL quote response: {row}")
            except Exception as exc:
                print(f"IIFL quote fetch error: {exc}")

        if results["iifl_oi"] is None and iifl_id:
            try:
                oi = iifl.get_oi(iifl_id)
                if oi is not None:
                    results["iifl_oi"] = oi
                    print(f"\n>>> IIFL parsed OI value: {oi}")
            except Exception as exc:
                print(f"IIFL OI fetch error: {exc}")

        if results["sharekhan_tick"] and results["iifl_quote"]:
            break
        time.sleep(3)

    print("\n=== SUMMARY ===")
    print(f"Sharekhan tick captured: {results['sharekhan_tick'] is not None}")
    print(f"IIFL quote captured: {results['iifl_quote'] is not None}")
    print(f"IIFL OI captured: {results['iifl_oi'] is not None}")
    if not results["sharekhan_tick"]:
        print("No Sharekhan tick received in time - check if the market is open, or look "
              "for the 'tick arrived as None' warning above (that's the binary-format signal).")


if __name__ == "__main__":
    main()
