"""
Runs as a SEPARATE scheduled process (cron/equivalent at 3:45 PM), not
inside the bot's main loop - locked design reasoning: if the bot crashes
mid-session, you especially want that day's report showing the crash, and
an in-loop report generator would never fire if the crashed process was
supposed to send it. This script reads only from disk (CSVs, state file,
log file), never from a live bot's memory.
"""
import csv
import os
import smtplib
import datetime
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

from config import Config
from state import StateManager

logger = logging.getLogger("daily_report")


def _read_csv_rows(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def _filter_today(rows, time_field, today: str):
    filtered = []
    for r in rows:
        raw = r.get(time_field)
        if not raw:
            continue
        try:
            ts = float(raw)
            date_str = datetime.datetime.fromtimestamp(ts).date().isoformat()
        except (ValueError, TypeError):
            date_str = raw[:10]
        if date_str == today:
            filtered.append(r)
    return filtered


def _summarize_trades(rows):
    if not rows:
        return {"trades": 0}
    pnls = [float(r["net_pnl"]) for r in rows if r.get("net_pnl") not in (None, "", "None")]
    if not pnls:
        return {"trades": len(rows), "note": "no closed trades with recorded PnL"}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    exit_reasons = {}
    for r in rows:
        reason = r.get("exit_reason") or "OPEN"
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
    return {
        "trades": len(pnls),
        "win_ratio_pct": round(len(wins) / len(pnls) * 100, 1) if pnls else 0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else "inf",
        "expectancy": round(sum(pnls) / len(pnls), 2),
        "net_pnl": round(sum(pnls), 2),
        "exit_reason_breakdown": exit_reasons,
    }


def _summarize_signal_audit(rows):
    total = len(rows)
    passed = sum(1 for r in rows if r.get("passed") in ("True", "true", True))
    block_reasons = {}
    for r in rows:
        if r.get("passed") not in ("True", "true", True):
            reason = r.get("block_reason") or "unknown"
            block_reasons[reason] = block_reasons.get(reason, 0) + 1
    return {"total_evaluated": total, "passed": passed, "block_reason_breakdown": block_reasons}


def _tail_log(path, n_lines=200):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        lines = f.readlines()
    return lines[-n_lines:]


def compose_report(cfg: Config, state: StateManager, feed_health=None):
    today = datetime.date.today().isoformat()
    mode = state.get("current_mode", cfg.default_mode)

    trade_log_path = cfg.live_trade_log if mode == "LIVE" else cfg.paper_trade_log
    trade_rows_today = _filter_today(_read_csv_rows(trade_log_path), "entry_time", today)
    trade_summary = _summarize_trades(trade_rows_today)

    audit_rows_today = _filter_today(_read_csv_rows(cfg.signal_audit_log), "timestamp", today)
    audit_summary = _summarize_signal_audit(audit_rows_today)

    error_lines = _tail_log(cfg.error_log)
    error_lines_today = [l for l in error_lines if today in l]
    exception_lines = [l for l in error_lines_today if "Traceback" in l or "CRITICAL" in l or "ERROR" in l]

    broker_health_lines = []
    if feed_health:
        for broker, count in feed_health.reconnect_counts_today.items():
            broker_health_lines.append(f"  {broker}: {count} reconnect(s) today")
        if not broker_health_lines:
            broker_health_lines.append("  No reconnects needed today.")

    body_lines = [
        f"=== Daily Trading Report - {today} ===",
        f"Mode: {mode}",
        "",
        "--- Executive Summary ---",
        f"Trades: {trade_summary.get('trades', 0)}",
    ]
    if trade_summary.get("trades", 0) > 0 and "win_ratio_pct" in trade_summary:
        body_lines += [
            f"Win ratio: {trade_summary['win_ratio_pct']}%",
            f"Profit factor: {trade_summary['profit_factor']}",
            f"Expectancy/trade: Rs {trade_summary['expectancy']}",
            f"Net PnL today: Rs {trade_summary['net_pnl']}",
            f"Exit reasons: {trade_summary['exit_reason_breakdown']}",
        ]

    body_lines += [
        "",
        "--- Signal Audit ---",
        f"Signals evaluated: {audit_summary['total_evaluated']}",
        f"Passed (traded): {audit_summary['passed']}",
        f"Blocked by: {audit_summary['block_reason_breakdown']}",
        "",
        "--- Broker Health ---",
    ] + broker_health_lines + [
        "",
        f"--- Errors today: {len(error_lines_today)} ---",
        f"--- Exceptions/critical today: {len(exception_lines)} ---",
        "(see attached error log for full detail)",
        "",
        f"--- Tomorrow's mode (persisted): {mode} ---",
    ]

    return "\n".join(body_lines), trade_rows_today


def send_daily_report(cfg: Config, state: StateManager, feed_health=None):
    body, trade_rows_today = compose_report(cfg, state, feed_health)

    msg = MIMEMultipart()
    msg["From"] = cfg.gmail_address
    msg["To"] = cfg.report_recipient or cfg.gmail_address
    msg["Subject"] = f"Nifty OI Bot - Daily Report {datetime.date.today().isoformat()}"
    msg.attach(MIMEText(body, "plain"))

    for path in (cfg.paper_trade_log, cfg.live_trade_log, cfg.signal_audit_log, cfg.error_log):
        if os.path.exists(path):
            with open(path, "rb") as f:
                part = MIMEApplication(f.read(), Name=os.path.basename(path))
            part["Content-Disposition"] = f'attachment; filename="{os.path.basename(path)}"'
            msg.attach(part)

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(cfg.gmail_address, cfg.gmail_app_password)
            server.send_message(msg)
        logger.info("Daily report sent.")
    except Exception as exc:
        logger.error("Failed to send daily report email: %s", exc)


if __name__ == "__main__":
    cfg = Config()
    state = StateManager(cfg.state_file)
    send_daily_report(cfg, state, feed_health=None)
