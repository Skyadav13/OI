# Nifty50 F&O OI-Buildup Trading Bot

Full production build across 8 phases: config/state/instance-lock foundation,
three-broker clients (Sharekhan primary data / IIFL secondary data / Kotak
execution-only), the OI-buildup + PCR signal engine with full noise-filter
chain, order lifecycle + exit stack trade manager, Telegram mode control
with kill switch, crash-recovery + degraded-mode resilience, and daily/weekly
reporting with human-gated analytics.

## Before running anything

1. **SEBI Algo-ID.** Register this strategy through Kotak's algo desk before
   ever using LIVE mode - this is a legal prerequisite, not a code setting.
   `main.py` has a placeholder `algo_id = None` at the order-placement call
   site; set it once registered.
2. **Verify the flagged unknowns.** Several response field names couldn't be
   confirmed without a live account (search your codebase for `VERIFY:` -
   found in `sharekhan_client.py` and `iifl_client.py`). Run once, inspect
   raw responses, correct the field names before trusting any output.
3. **Paper trade first, for real.** Minimum 15-20 trading days before
   `weekly_analytics.py` will even produce a recommendation - that's not
   arbitrary, it's the actual validation gate for this strategy.

## Setup
```bash
pip install -r requirements.txt
cp .env.example .env   # fill in, then `source .env` or use your process manager's env loading
python main.py
```

## Running continuously
`main.py` is a long-running poll loop - it does not daemonize itself. Run it
under a process supervisor that restarts on crash (this is required for the
self-heal design to actually work - the bot cannot restart itself if its own
process dies):
```ini
# systemd unit example
[Service]
ExecStart=/usr/bin/python3 /path/to/main.py
Restart=on-failure
RestartSec=10
```

## Daily report
`daily_report.py` is a **separate** script, not part of the main loop -
schedule it independently:
```bash
45 15 * * 1-5 /usr/bin/python3 /path/to/daily_report.py
```

## Weekly analytics
Not wired to a scheduler in this build - run manually or add your own cron
entry calling `weekly_analytics.analyze()` / `format_telegram_message()` /
`telegram.send()` on whatever cadence you prefer (weekly, per locked design).

## Telegram commands
- `PAPER` - immediate switch, no friction
- `LIVE` - requires `CONFIRM` within 5 minutes
- `CONFIRM` - confirms a pending LIVE request
- `STOP` - kill switch: halts entries, force-closes any open position
- `RESUME` - clears STOP
- `APPLY <param> <value>` - applies a bounds-checked parameter change,
  audit-logged, persists across restarts

## File map by phase
| Phase | Files |
|---|---|
| 1 - Foundation | `config.py`, `state.py`, `instance_lock.py`, `instruments.py`, `logging_setup.py` |
| 2 - Broker clients | `sharekhan_client.py`, `iifl_client.py`, `kotak_client.py`, `iifl_crypto.py`, `totp.py` |
| 3 - Signal engine | `oi_engine.py`, `bar_aggregator.py`, `vix_monitor.py` |
| 4 - Trade manager | `trade_manager.py`, `options_cost_model.py` |
| 5 - Risk & mode | `mode_controller.py`, `telegram_client.py` |
| 6 - Resilience | `feed_health.py`, `degraded_mode.py`, `reconciliation.py` |
| 7 - Reporting | `signal_audit_logger.py`, `param_bounds.py`, `daily_report.py`, `config_overrides.py`, `weekly_analytics.py` |
| 8 - Orchestration | `main.py`, `expiry_utils.py` |
| 9 - Pre-flight safety & liveness | `heartbeat.py`, `heartbeat_check.py`, margin/circuit checks in `kotak_client.py`/`trade_manager.py`, slippage tracking in `trade_manager.py` |

## Deployment
See `deploy/DEPLOY.md` for the full VM setup walkthrough - systemd unit
(`deploy/nifty-oi-bot.service`) for the main process, crontab entries
(`deploy/crontab.example`) for the daily report and dead-man's switch.

## Known limitations, stated plainly
- Sharekhan/IIFL response field names for quotes/OI verified against
  official request schemas only, not live responses - see `VERIFY:` comments.
- Kotak client (`kotak_client.py`) rebuilt against the real kotak-neo-python
  v3.0.x source (uploaded repo, not just release notes) - confirmed field
  names throughout, including margin/circuit-limit checks added later.
- SEBI Algo-ID: per your confirmation, individual/self-use retail trading
  doesn't require registration - worth a direct check with Kotak's support
  before LIVE, since regulatory carve-outs sometimes have fine print (order
  rate thresholds etc.), but not currently blocking.
- The OI-wall target uses an assumed 0.5 delta to translate a strike-level
  wall into a premium-scale target - a simplification, not a real options
  pricing model (caught and fixed during integration testing, see git-style
  comments in `trade_manager.py` around `_wall_strike_to_premium_target`).
- A resumed-after-crash position's OI-reversal confirmation window restarts
  fresh (classification history doesn't survive a restart) - see
  `reconciliation.py` docstring.
- Exchange-holiday-adjusted expiry shifts rely entirely on the broker's own
  contract master being current - no independent holiday calendar.
- Weekly analytics isn't wired to an automatic scheduler in this build.
- The dead-man's switch (`heartbeat_check.py`), as shipped, assumes cron is
  running on the SAME VM as the bot - genuine redundancy against a full VM
  outage needs it running from a different host. See its docstring.
- Margin check assumes MKT orders (price passed as the live quote's LTP,
  not a limit price) - if the order type strategy ever changes to include
  limit orders, this check's price parameter needs revisiting.

## Testing performed
Every phase was smoke-tested with synthetic/fake broker objects during
development (not against live accounts, which weren't available) - full
lifecycle paths verified: signal generation through all filters, order
open/exit through all four exit reasons, mode switching, kill switch, day
rollover, feed health/degraded mode, reconciliation, daily report
composition, and weekly recommendation generation. Two real bugs (wall
target scale mismatch, wall-below-spot edge case) were caught and fixed by
this testing before ever touching a real account - a reminder that paper
mode remains essential even after all of this.
