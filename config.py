import os
from dataclasses import dataclass


@dataclass
class Config:
    # ================= Broker roles =================
    # Sharekhan = primary data, IIFL = secondary/cross-check, Kotak = execution-only
    sharekhan_api_key: str = os.getenv("SHAREKHAN_API_KEY", "")
    sharekhan_customer_id: str = os.getenv("SHAREKHAN_CUSTOMER_ID", "")
    sharekhan_login_id: str = os.getenv("SHAREKHAN_LOGIN_ID", "")
    sharekhan_password: str = os.getenv("SHAREKHAN_PASSWORD", "")
    sharekhan_totp_secret: str = os.getenv("SHAREKHAN_TOTP_SECRET", "")
    sharekhan_secret_key: str = os.getenv("SHAREKHAN_SECRET_KEY", "")

    iifl_app_key: str = os.getenv("IIFL_APP_KEY", "")
    iifl_client_code: str = os.getenv("IIFL_CLIENT_CODE", "")
    iifl_password: str = os.getenv("IIFL_PASSWORD", "")
    iifl_app_secret: str = os.getenv("IIFL_APP_SECRET", "")
    iifl_totp_secret: str = os.getenv("IIFL_TOTP_SECRET", "")

    kotak_consumer_key: str = os.getenv("KOTAK_CONSUMER_KEY", "")
    kotak_mobile: str = os.getenv("KOTAK_MOBILE", "")
    kotak_ucc: str = os.getenv("KOTAK_UCC", "")
    kotak_mpin: str = os.getenv("KOTAK_MPIN", "")
    kotak_totp_secret: str = os.getenv("KOTAK_TOTP_SECRET", "")   # for automated headless login, matches Sharekhan/IIFL pattern

    telegram_token: str = os.getenv("TELEGRAM_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    gmail_address: str = os.getenv("GMAIL_ADDRESS", "")
    gmail_app_password: str = os.getenv("GMAIL_APP_PASSWORD", "")
    report_recipient: str = os.getenv("REPORT_RECIPIENT", "")

    # ================= Instrument =================
    underlying_symbol: str = "NIFTY"
    strike_step: int = 50
    strikes_around_atm_pcr: int = 3
    strikes_around_atm_wall: int = 7
    lot_size: int = 75          # verify current NSE lot size before trading

    # ================= Signal thresholds =================
    lookback_bars: int = 15
    price_thresh_pct: float = 0.15
    oi_thresh_pct: float = 2.0
    min_absolute_oi: float = 500000
    persistence_polls: int = 3
    pcr_bullish_threshold: float = 1.2
    pcr_bearish_threshold: float = 0.8

    # ================= Noise filters =================
    whipsaw_lookback_polls: int = 20
    whipsaw_max_flips: int = 2
    atr_period: int = 14
    atr_floor_multiplier: float = 0.8
    atr_rolling_mean_polls: int = 50
    gap_open_exclude_minutes: int = 15
    cross_feed_disagreement_tolerance_pct: float = 5.0   # OI/LTP gap beyond this = disagreement
    option_leg_staleness_max_polls: int = 3               # OI unchanged this many polls = stale

    # ================= Session / expiry timing =================
    session_start: str = "09:20"
    session_end: str = "15:15"
    high_conf_only_after: str = "13:00"
    no_new_entry_after: str = "14:00"

    # ================= Exit logic =================
    reversal_confirm_polls: int = 2
    reversal_min_dwell_polls: int = 2
    trail_activate_at_pct_of_target: float = 0.5
    trail_lock_fraction: float = 0.5
    catastrophic_stop_pct: float = 45.0     # wide backstop, % of premium
    wall_recompute_minutes: int = 7

    # ================= Risk controls =================
    max_open_positions: int = 1
    cooldown_minutes_after_stop: int = 12
    max_trades_per_day: int = 4
    daily_loss_limit_pct: float = 3.0
    conviction_base_lots: int = 1
    conviction_max_lots: int = 3
    wall_target_assumed_delta: float = 0.5   # rough ATM delta used to translate a strike-price wall into a premium target

    # ================= Costs - OPTIONS rates (the bot trades CE/PE, not
    # futures - these differ meaningfully from futures rates, confirmed
    # post-April-2026 STT hike; verify periodically, rates do change) =====
    brokerage_per_order: float = 0.0
    stt_sell_pct: float = 0.15            # % of premium, sell side (squared off intraday, not exercised)
    stamp_duty_buy_pct: float = 0.003     # % of premium, buy side
    exchange_txn_pct: float = 0.035       # ~Rs 35.03 per lakh of premium value, materially higher than futures
    sebi_fee_pct: float = 0.0001
    gst_pct: float = 18.0
    slippage_pct: float = 0.05

    # ================= Mode / lifecycle =================
    default_mode: str = "PAPER"
    poll_interval_seconds: int = 30
    mode_decision_time: str = "09:05"
    mode_decision_timeout: str = "09:12"

    # ================= Files =================
    state_file: str = "bot_state.json"
    lock_file: str = "bot.lock"
    paper_trade_log: str = "paper_trades.csv"
    live_trade_log: str = "live_trades.csv"
    signal_audit_log: str = "signal_audit.csv"
    error_log: str = "errors.log"
    daily_report_dir: str = "reports"

    heartbeat_file: str = "bot_heartbeat.txt"

    # ================= Self-heal =================
    heartbeat_timeout_seconds: int = 90        # 3x poll interval
    reconnect_backoff_seconds: tuple = (5, 15, 45)
