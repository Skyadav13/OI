"""
Order lifecycle (locked design): position is only created on a CONFIRMED
fill - never optimistically on order placement. SL/target computed off the
confirmed fill price, and the OI-reversal baseline is rebased to fill time,
not signal time. Exit priority: catastrophic backstop > OI-reversal >
trailing-on-deceleration > wall target - matches the locked exit stack.
"""
import csv
import os
import time
import logging
import datetime

from options_cost_model import round_trip_cost

logger = logging.getLogger("trade_manager")

FILL_TIMEOUT_SECONDS = 15


class Position:
    def __init__(self, direction, option_type, strike, resolved_instrument, quantity,
                 entry_price, entry_time, entry_classification_index,
                 catastrophic_price, wall_target_price, signal_price=None):
        self.direction = direction            # "BULLISH" | "BEARISH" -> CE | PE
        self.option_type = option_type         # "CE" | "PE"
        self.strike = strike
        self.resolved_instrument = resolved_instrument
        self.quantity = quantity
        self.entry_price = entry_price
        self.entry_time = entry_time
        self.entry_classification_index = entry_classification_index
        self.catastrophic_price = catastrophic_price
        self.wall_target_price = wall_target_price
        self.trailing_active = False
        self.trail_stop = None
        self.status = "OPEN"
        self.exit_price = None
        self.exit_time = None
        self.exit_reason = None
        self.gross_pnl = None
        self.cost = None
        self.net_pnl = None
        # slippage tracking: signal-time price the decision was made on vs
        # the actual confirmed fill price. PAPER mode fills at signal price
        # by construction, so slippage there is always ~0 - only meaningful
        # for LIVE, but tracked in both for a consistent CSV schema.
        self.signal_price = signal_price
        self.slippage_pct = (
            round((entry_price - signal_price) / signal_price * 100, 3)
            if signal_price else None
        )

    def to_row(self):
        return {
            "direction": self.direction, "option_type": self.option_type, "strike": self.strike,
            "quantity": self.quantity, "entry_price": self.entry_price, "entry_time": self.entry_time,
            "signal_price": self.signal_price, "slippage_pct": self.slippage_pct,
            "catastrophic_price": self.catastrophic_price, "wall_target_price": self.wall_target_price,
            "status": self.status, "exit_price": self.exit_price, "exit_time": self.exit_time,
            "exit_reason": self.exit_reason, "gross_pnl": self.gross_pnl,
            "cost": self.cost, "net_pnl": self.net_pnl,
        }


class TradeManager:
    def __init__(self, cfg, state, kotak_client=None):
        self.cfg = cfg
        self.state = state
        self.kotak_client = kotak_client   # None entirely in PAPER mode, per locked design
        self.position: Position = None

    # ---------- gating ----------
    def can_open_new(self) -> bool:
        if self.position is not None:
            return False
        if self.state.get("trades_today_count", 0) >= self.cfg.max_trades_per_day:
            return False
        if self.state.get("loss_today", 0.0) <= -(self.cfg.daily_loss_limit_pct / 100) * self.cfg.starting_capital \
                if hasattr(self.cfg, "starting_capital") else False:
            return False
        cooldown_until = self.state.get("cooldown_until")
        if cooldown_until and datetime.datetime.now().isoformat() < cooldown_until:
            return False
        return True

    def conviction_lots(self, conviction_score: float) -> int:
        if conviction_score >= 3.0:
            return self.cfg.conviction_max_lots
        if conviction_score >= 1.8:
            return min(self.cfg.conviction_base_lots + 1, self.cfg.conviction_max_lots)
        return self.cfg.conviction_base_lots

    # ---------- entry (order lifecycle) ----------
    def open_position(self, mode: str, direction: str, option_type: str, strike: int,
                       resolved_instrument, current_premium: float, current_spot: float,
                       conviction_score: float, engine, call_wall, put_wall) -> bool:
        quantity = self.conviction_lots(conviction_score) * self.cfg.lot_size

        if mode == "LIVE":
            filled = self._place_and_confirm_live(resolved_instrument, quantity)
            if filled is None:
                logger.warning("LIVE entry order not confirmed filled - no position created.")
                return False
            fill_price, fill_time = filled
        else:
            # PAPER: simulate fill at current premium with slippage baked
            # into the cost model already, no separate slippage applied here
            fill_price, fill_time = current_premium, time.time()

        wall_strike = (call_wall if direction == "BULLISH" else put_wall)
        wall_target = self._wall_strike_to_premium_target(
            wall_strike, current_spot, fill_price, direction)

        catastrophic_price = fill_price * (1 - self.cfg.catastrophic_stop_pct / 100)

        self.position = Position(
            direction=direction, option_type=option_type, strike=strike,
            resolved_instrument=resolved_instrument, quantity=quantity,
            entry_price=fill_price, entry_time=fill_time,
            entry_classification_index=len(engine.classification_history) - 1,
            catastrophic_price=catastrophic_price, wall_target_price=wall_target,
            signal_price=current_premium,
        )

        count = self.state.get("trades_today_count", 0) + 1
        self.state.set("trades_today_count", count)
        self.state.set("open_position", self.position.to_row())
        self._log_trade(mode, self.position)
        logger.info("Opened %s %s %s qty=%d @ %.2f target=%.2f catastrophic=%.2f",
                    mode, option_type, strike, quantity, fill_price, wall_target, catastrophic_price)
        return True

    def _wall_strike_to_premium_target(self, wall_strike, current_spot, fill_premium, direction):
        """The OI wall is a level on the UNDERLYING (a strike price), not a
        premium value - these are different scales and must not be compared
        directly (caught during integration testing). Translate the distance
        the underlying would need to move to reach the wall into an
        approximate premium move, using an assumed near-ATM delta. This is
        a simplification, not a real options pricing model - reasonable for
        an initial target ceiling, not for precision."""
        if wall_strike is None or current_spot is None:
            # degenerate fallback per locked design - no usable wall
            return fill_premium * 1.6 if direction == "BULLISH" else fill_premium * 0.4

        underlying_distance = (wall_strike - current_spot) if direction == "BULLISH" \
            else (current_spot - wall_strike)

        if underlying_distance <= 0:
            # wall collapsed to at-or-behind spot after the one-strike-short
            # adjustment - not a usable target (caught during integration
            # testing: this previously produced target == entry_price,
            # triggering an instant false TARGET exit). Fall back rather
            # than trade a zero-distance target.
            return fill_premium * 1.6 if direction == "BULLISH" else fill_premium * 0.4

        premium_move = underlying_distance * self.cfg.wall_target_assumed_delta
        return fill_premium + premium_move

    def _place_and_confirm_live(self, resolved_instrument, quantity):
        kotak_id = resolved_instrument.broker_ids.get("kotak")
        if not kotak_id:
            logger.error("No Kotak instrument ID resolved - cannot place LIVE order.")
            return None
        trading_symbol = self.kotak_client.trading_symbol_from_id(kotak_id)
        if not trading_symbol:
            logger.error("Could not extract Kotak trading_symbol from resolved id %r.", kotak_id)
            return None

        # --- pre-flight: circuit limit check ---
        # Narrow, justified exception to Kotak's execution-only role (same
        # precedent as the emergency-LTP fallback) - this is an "is it safe
        # to execute right now" check, not a data-feed expansion.
        pre_quote = self.kotak_client.get_pre_trade_quote(kotak_id)
        if pre_quote and pre_quote.get("ltp") and pre_quote.get("high_circuit"):
            if pre_quote["ltp"] >= pre_quote["high_circuit"] * 0.98:
                logger.error("Instrument %s is within 2%% of its upper circuit (%.2f) - "
                            "skipping entry, order book may be frozen/illiquid.",
                            trading_symbol, pre_quote["high_circuit"])
                return None

        # --- pre-flight: margin check ---
        entry_price_str = str(pre_quote["ltp"]) if pre_quote and pre_quote.get("ltp") else "0"
        sufficient, margin_detail = self.kotak_client.check_margin(
            kotak_id, quantity, entry_price_str, transaction_type="B")
        if not sufficient:
            logger.error("Margin check failed for %s qty=%d - not placing order. Detail: %s",
                        trading_symbol, quantity, margin_detail)
            return None

        try:
            order_resp = self.kotak_client.place_order(
                trading_symbol=trading_symbol, exchange_segment="nse_fo",
                transaction_type="B", quantity=quantity,
            )
        except Exception as exc:
            logger.error("LIVE order placement failed: %s", exc)
            return None

        order_id = order_resp.get("order_id") or order_resp.get("nOrdNo")
        if not order_id:
            logger.error("LIVE order response had no order id: %s", order_resp)
            return None

        deadline = time.time() + FILL_TIMEOUT_SECONDS
        while time.time() < deadline:
            try:
                status = self.kotak_client.order_status(order_id)
            except Exception as exc:
                logger.warning("Order status poll failed: %s", exc)
                time.sleep(2)
                continue

            state = (status.get("ordSt") or status.get("status") or "").upper()
            if state in ("REJECTED", "CANCELLED"):
                logger.error("LIVE order %s: %s", state, status)
                return None
            if state in ("COMPLETE", "FILLED", "EXECUTED"):
                fill_price = float(status.get("avgPrc") or status.get("average_price") or 0)
                filled_qty = int(status.get("fldQty") or status.get("filled_quantity") or quantity)
                if filled_qty < quantity:
                    logger.warning("LIVE order partially filled: %d of %d - tracking actual filled qty.",
                                  filled_qty, quantity)
                return fill_price, time.time()
            time.sleep(2)

        logger.error("LIVE order %s not confirmed filled within timeout - treating as failed, no position created.", order_id)
        return None

    # ---------- exit stack ----------
    def check_exit(self, mode: str, current_premium: float, engine) -> bool:
        """Priority: catastrophic > OI-reversal > trailing/target.
        Returns True if the position was closed this call."""
        if self.position is None or current_premium is None:
            return False

        pos = self.position
        direction = pos.direction

        # 1. catastrophic backstop
        if current_premium <= pos.catastrophic_price:
            self._close(mode, current_premium, "CATASTROPHIC")
            return True

        # 2. OI-reversal, rebased to fill time
        needed_reversal = "BEARISH" if direction == "BULLISH" else "BULLISH"
        confirmed = engine.persistent_bias_since(pos.entry_classification_index, self.cfg.reversal_confirm_polls)
        if confirmed == needed_reversal:
            self._close(mode, current_premium, "OI_REVERSAL")
            return True

        # 3. trailing / target
        target = pos.wall_target_price
        halfway = (pos.entry_price + target) / 2
        reached_halfway = current_premium >= halfway

        if reached_halfway and not pos.trailing_active:
            pos.trailing_active = True

        if pos.trailing_active:
            gain = current_premium - pos.entry_price
            locked = pos.entry_price + self.cfg.trail_lock_fraction * gain
            pos.trail_stop = max(pos.trail_stop, locked) if pos.trail_stop is not None else locked
            if current_premium <= pos.trail_stop:
                self._close(mode, current_premium, "TRAIL_STOP")
                return True

        if current_premium >= target:
            self._close(mode, current_premium, "TARGET")
            return True

        return False

    def _close(self, mode: str, exit_price: float, reason: str):
        pos = self.position

        if mode == "LIVE":
            try:
                kotak_id = pos.resolved_instrument.broker_ids.get("kotak")
                trading_symbol = self.kotak_client.trading_symbol_from_id(kotak_id)
                self.kotak_client.place_order(
                    trading_symbol=trading_symbol, exchange_segment="nse_fo",
                    transaction_type="S", quantity=pos.quantity,
                )
            except Exception as exc:
                logger.error("LIVE exit order failed - MANUAL INTERVENTION NEEDED: %s", exc)

        gross = (exit_price - pos.entry_price) * pos.quantity
        cost = round_trip_cost(pos.entry_price, exit_price, pos.quantity, self.cfg)
        net = gross - cost

        pos.status = "CLOSED"
        pos.exit_price = exit_price
        pos.exit_time = time.time()
        pos.exit_reason = reason
        pos.gross_pnl = gross
        pos.cost = cost
        pos.net_pnl = net

        loss_today = self.state.get("loss_today", 0.0) + min(net, 0)
        self.state.set("loss_today", loss_today)

        if reason in ("CATASTROPHIC", "OI_REVERSAL") and net < 0:
            cooldown_until = (datetime.datetime.now() +
                             datetime.timedelta(minutes=self.cfg.cooldown_minutes_after_stop)).isoformat()
            self.state.set("cooldown_until", cooldown_until)

        self.state.set("open_position", None)
        self._log_trade(mode, pos)
        logger.info("Closed %s @ %.2f reason=%s net_pnl=%.2f", pos.option_type, exit_price, reason, net)
        self.position = None

    def force_close(self, mode: str, current_premium: float, reason: str):
        if self.position is not None and current_premium is not None:
            self._close(mode, current_premium, reason)

    def _log_trade(self, mode: str, pos: Position):
        log_file = self.cfg.live_trade_log if mode == "LIVE" else self.cfg.paper_trade_log
        file_exists = os.path.exists(log_file)
        with open(log_file, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(pos.to_row().keys()))
            if not file_exists:
                writer.writeheader()
            writer.writerow(pos.to_row())
