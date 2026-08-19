"""
Entry point. Startup sequence:
  1. Config + overrides + logging + instance lock
  2. Load state, determine persisted mode (PAPER default)
  3. Kotak instantiated/logged-in ONLY if mode == LIVE - fully absent from
     the PAPER code path, per locked design
  4. Sharekhan (primary) + IIFL (secondary) always login - data feeds
     needed regardless of mode
  5. Startup reconciliation against live Kotak state (LIVE mode only)
  6. Dynamic expiry detection, instrument resolution across all brokers
  7. Poll loop: mode/kill-switch commands -> data collection -> signal
     evaluation -> exit checks -> entry checks -> sleep
Every loop iteration is wrapped in try/except - a single bad cycle logs
and continues, it does not crash the process (self-heal). A genuinely
fatal startup error is expected to crash and be restarted by an external
watchdog (systemd/cron), which is outside what this script can do for
itself, per locked design.
"""
import time
import datetime
import logging

from config import Config
from config_overrides import apply_overrides
from logging_setup import setup_logging
from instance_lock import InstanceLock
from state import StateManager
from telegram_client import TelegramClient
from mode_controller import ModeController
from sharekhan_client import SharekhanClient
from iifl_client import IIFLClient
from kotak_client import KotakClient
from instruments import Descriptor, InstrumentResolver
from expiry_utils import get_calc_and_trade_expiry
from oi_engine import OIEngine
from vix_monitor import VixMonitor
from trade_manager import TradeManager
from feed_health import FeedHealthMonitor, retry_with_backoff
from degraded_mode import DegradedModeController
from reconciliation import reconcile_on_startup
from signal_audit_logger import SignalAuditLogger
from heartbeat import write_heartbeat

logger = logging.getLogger("main")


def within_session(cfg) -> bool:
    now = datetime.datetime.now().strftime("%H:%M")
    return cfg.session_start <= now <= cfg.session_end


def nearest_strike(spot: float, step: int) -> int:
    return int(round(spot / step) * step)


def run():
    cfg = Config()
    cfg = apply_overrides(cfg)
    setup_logging(cfg.error_log)
    logger.info("=== Nifty OI Bot starting ===")

    lock = InstanceLock(cfg.lock_file)
    lock.acquire()

    try:
        _run_inner(cfg)
    finally:
        lock.release()


def _run_inner(cfg):
    state = StateManager(cfg.state_file)
    telegram = TelegramClient(cfg)
    mode_controller = ModeController(cfg, state, telegram)
    mode_controller.check_day_rollover()
    mode = mode_controller.current_mode()
    logger.info("Persisted mode: %s", mode)

    # ---------- broker logins ----------
    sharekhan = SharekhanClient(cfg)
    iifl = IIFLClient(cfg)
    kotak = None

    if not sharekhan.login():
        telegram.send("⚠️ Sharekhan (primary data) login failed at startup. Check credentials/2FA.")
    if not iifl.login():
        telegram.send("⚠️ IIFL (secondary data) login failed at startup. Check credentials/2FA.")

    if mode == "LIVE":
        import pyotp   # lazy import - only needed on the LIVE path, keeps PAPER mode dependency-light
        kotak = KotakClient(cfg)
        try:
            totp = pyotp.TOTP(cfg.kotak_totp_secret).now()
            kotak.login(totp)
        except Exception as exc:
            telegram.send(f"🚨 LIVE mode but Kotak login failed: {exc}. Cannot place real orders. Halting startup.")
            logger.error("Kotak login failed in LIVE mode: %s", exc)
            return
        telegram.send("Kotak (execution) logged in - LIVE mode active. Confirm SEBI Algo-ID is registered "
                      "before this places real orders.")
    else:
        logger.info("PAPER mode - Kotak client not instantiated, per locked design.")

    reconcile_on_startup(mode, state, kotak, telegram)

    # ---------- dynamic expiry + instrument resolution ----------
    try:
        expiries = iifl.get_nifty_futures_expiries(cfg.underlying_symbol)
        calc_expiry, trade_expiry, is_expiry_day = get_calc_and_trade_expiry(expiries)
    except Exception as exc:
        telegram.send(f"🚨 Could not determine expiry dates: {exc}. Cannot proceed.")
        logger.error("Expiry detection failed: %s", exc)
        return

    logger.info("calc_expiry=%s trade_expiry=%s is_expiry_day=%s", calc_expiry, trade_expiry, is_expiry_day)

    brokers = {"sharekhan": sharekhan, "iifl": iifl, "kotak": kotak}
    resolver = InstrumentResolver(brokers)

    calc_fut_descriptor = Descriptor(cfg.underlying_symbol, "FUT", calc_expiry)
    calc_fut = resolver.resolve(calc_fut_descriptor)

    fut_sk_id = calc_fut.broker_ids.get("sharekhan")
    fut_iifl_id = calc_fut.broker_ids.get("iifl")
    if not fut_sk_id and not fut_iifl_id:
        telegram.send("🚨 Could not resolve NIFTY futures on either data broker. Cannot proceed.")
        return

    if fut_sk_id:
        sharekhan.subscribe([fut_sk_id])

    # bootstrap spot estimate to compute ATM strike range
    spot_guess = sharekhan.get_ltp(fut_sk_id) if fut_sk_id else None
    if spot_guess is None:
        time.sleep(3)   # give the websocket a moment to deliver a first tick
        spot_guess = sharekhan.get_ltp(fut_sk_id) if fut_sk_id else None
    if spot_guess is None and fut_iifl_id:
        spot_guess = iifl.get_ltp(fut_iifl_id)
    if spot_guess is None:
        telegram.send("🚨 Could not bootstrap a spot price from either feed. Cannot proceed.")
        return

    atm = nearest_strike(spot_guess, cfg.strike_step)
    wall_range = cfg.strikes_around_atm_wall
    strikes = [atm + i * cfg.strike_step for i in range(-wall_range, wall_range + 1)]

    option_resolved = {}   # {(strike, type): ResolvedInstrument}
    sk_option_ids = []
    for strike in strikes:
        for opt_type in ("CE", "PE"):
            desc = Descriptor(cfg.underlying_symbol, opt_type, trade_expiry, strike)
            resolved = resolver.resolve(desc)
            option_resolved[(strike, opt_type)] = resolved
            if resolved.broker_ids.get("sharekhan"):
                sk_option_ids.append(resolved.broker_ids["sharekhan"])

    if sk_option_ids:
        sharekhan.subscribe(sk_option_ids)

    logger.info("Resolved futures + %d option legs across strikes %s", len(option_resolved), strikes)

    # ---------- engine + supporting components ----------
    engine = OIEngine(cfg)
    engine.mark_session_start()
    vix = VixMonitor()
    trade_manager = TradeManager(cfg, state, kotak_client=kotak)
    feed_health = FeedHealthMonitor(cfg)
    degraded = DegradedModeController(telegram)
    audit_logger = SignalAuditLogger(cfg.signal_audit_log)

    telegram.send(f"Bot started. Mode={mode}. ATM~{atm}. calc_expiry={calc_expiry} trade_expiry={trade_expiry} "
                 f"is_expiry_day={is_expiry_day}.")

    # ---------- main poll loop ----------
    while True:
        try:
            _poll_cycle(cfg, state, mode_controller, sharekhan, iifl, kotak, calc_fut,
                       option_resolved, strikes, atm, engine, vix, trade_manager,
                       feed_health, degraded, audit_logger, telegram)
        except Exception as exc:
            logger.exception("Unhandled exception in poll cycle (continuing): %s", exc)

        # Written every cycle regardless of whether it errored - proves the
        # process itself is alive and looping, distinct from feed_health
        # (which tracks broker data staleness, not process liveness).
        write_heartbeat(cfg.heartbeat_file)

        time.sleep(cfg.poll_interval_seconds)


def _poll_cycle(cfg, state, mode_controller, sharekhan, iifl, kotak, calc_fut,
                option_resolved, strikes, atm, engine, vix, trade_manager,
                feed_health, degraded, audit_logger, telegram):

    mode_controller.poll_and_dispatch()
    mode_controller.check_day_rollover()
    mode = mode_controller.current_mode()

    if mode_controller.is_stopped():
        if trade_manager.position is not None:
            fut_sk_id = calc_fut.broker_ids.get("sharekhan")
            premium = sharekhan.get_ltp(trade_manager.position.resolved_instrument.broker_ids.get("sharekhan"))
            trade_manager.force_close(mode, premium, "KILL_SWITCH")
        return

    if not within_session(cfg):
        return

    time_str = datetime.datetime.now().strftime("%H:%M")
    fut_sk_id = calc_fut.broker_ids.get("sharekhan")
    fut_iifl_id = calc_fut.broker_ids.get("iifl")

    sk_ltp = sharekhan.get_ltp(fut_sk_id) if fut_sk_id else None
    sk_oi = sharekhan.get_oi(fut_sk_id) if fut_sk_id else None
    if sk_ltp is not None:
        feed_health.mark_success("sharekhan")

    iifl_ltp = retry_with_backoff(iifl.get_ltp, cfg.reconnect_backoff_seconds, "iifl", feed_health, fut_iifl_id) \
        if fut_iifl_id else None
    iifl_oi = retry_with_backoff(iifl.get_oi, cfg.reconnect_backoff_seconds, "iifl", feed_health, fut_iifl_id) \
        if fut_iifl_id else None

    engine.update_futures(sk_ltp, sk_oi, iifl_ltp, iifl_oi)

    call_ois, put_ois = {}, {}
    for strike in strikes:
        for opt_type, target_dict in (("CE", call_ois), ("PE", put_ois)):
            resolved = option_resolved.get((strike, opt_type))
            if not resolved:
                continue
            sk_id = resolved.broker_ids.get("sharekhan")
            iifl_id = resolved.broker_ids.get("iifl")
            sk_leg_oi = sharekhan.get_oi(sk_id) if sk_id else None
            iifl_leg_oi = iifl.get_oi(iifl_id) if iifl_id else None
            engine.update_option_leg(strike, opt_type, sk_leg_oi, iifl_leg_oi)
            target_dict[strike] = engine.get_option_oi(strike, opt_type)

    current_oi = engine.fut_history[-1][2] if engine.fut_history else None
    result = engine.evaluate(
        current_oi=current_oi, time_str=time_str,
        call_ois={k: v for k, v in call_ois.items() if abs(k - atm) <= cfg.strikes_around_atm_pcr * cfg.strike_step},
        put_ois={k: v for k, v in put_ois.items() if abs(k - atm) <= cfg.strikes_around_atm_pcr * cfg.strike_step},
        vix_elevated=vix.is_elevated(), atm_strike=atm,
    )
    audit_logger.log(result)

    has_position = trade_manager.position is not None
    degraded_active = degraded.evaluate(feed_health, has_position)

    if has_position:
        pos = trade_manager.position
        sk_opt_id = pos.resolved_instrument.broker_ids.get("sharekhan")
        iifl_opt_id = pos.resolved_instrument.broker_ids.get("iifl")

        if degraded_active:
            kotak_id = pos.resolved_instrument.broker_ids.get("kotak")
            emergency_ltp = degraded.get_emergency_ltp(kotak, kotak_id)
            if emergency_ltp is not None and emergency_ltp <= pos.catastrophic_price:
                trade_manager.force_close(mode, emergency_ltp, "CATASTROPHIC")
        else:
            sk_premium = sharekhan.get_ltp(sk_opt_id) if sk_opt_id else None
            iifl_premium = iifl.get_ltp(iifl_opt_id) if iifl_opt_id else None
            premium, _ = engine._reconcile(sk_premium, iifl_premium, "option_premium")
            trade_manager.check_exit(mode, premium, engine)
        return

    if degraded_active:
        return   # no new entries while degraded, per locked design

    if not result.passed or not trade_manager.can_open_new():
        return

    option_type = "CE" if result.direction == "BULLISH" else "PE"
    resolved = option_resolved.get((atm, option_type))
    if not resolved:
        logger.warning("No resolved instrument for ATM %s %s - skipping entry.", atm, option_type)
        return

    sk_id = resolved.broker_ids.get("sharekhan")
    iifl_id = resolved.broker_ids.get("iifl")
    sk_premium = sharekhan.get_ltp(sk_id) if sk_id else None
    iifl_premium = iifl.get_ltp(iifl_id) if iifl_id else None
    entry_premium, _ = engine._reconcile(sk_premium, iifl_premium, "entry_premium")
    if entry_premium is None:
        logger.warning("No usable premium to enter %s %s - skipping.", atm, option_type)
        return

    current_spot, _ = engine._reconcile(
        sharekhan.get_ltp(calc_fut.broker_ids.get("sharekhan")),
        iifl.get_ltp(calc_fut.broker_ids.get("iifl")), "spot_for_wall_target",
    )
    call_wall, put_wall = engine.compute_wall(atm, call_ois, put_ois, entry_strike=atm, current_spot=current_spot)

    trade_manager.open_position(
        mode=mode, direction=result.direction, option_type=option_type, strike=atm,
        resolved_instrument=resolved, current_premium=entry_premium, current_spot=current_spot,
        conviction_score=result.conviction_score, engine=engine,
        call_wall=call_wall, put_wall=put_wall,
    )


if __name__ == "__main__":
    run()
