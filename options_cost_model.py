def round_trip_cost(entry_premium: float, exit_premium: float, quantity: int, cfg) -> float:
    """Options-specific costs - materially different from futures (see
    config.py comment): STT and exchange charges are on premium value,
    at higher rates than futures. Brokerage fixed at 0 (confirmed Kotak
    Neo API rate). Position is always bought then sold same-day (never
    exercised), so STT applies to the sell-side premium only.
    """
    buy_turnover = entry_premium * quantity
    sell_turnover = exit_premium * quantity

    brokerage = cfg.brokerage_per_order * 2
    stt = sell_turnover * (cfg.stt_sell_pct / 100)
    stamp_duty = buy_turnover * (cfg.stamp_duty_buy_pct / 100)
    exchange_txn = (buy_turnover + sell_turnover) * (cfg.exchange_txn_pct / 100)
    sebi_fee = (buy_turnover + sell_turnover) * (cfg.sebi_fee_pct / 100)
    gst = (brokerage + exchange_txn) * (cfg.gst_pct / 100)
    slippage = (buy_turnover + sell_turnover) * (cfg.slippage_pct / 100)

    return brokerage + stt + stamp_duty + exchange_txn + sebi_fee + gst + slippage
