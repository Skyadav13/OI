import csv
import os
import datetime


class SignalAuditLogger:
    def __init__(self, path: str):
        self.path = path

    def log(self, signal_result):
        row = {
            "timestamp": datetime.datetime.fromtimestamp(signal_result.timestamp).isoformat(),
            "passed": signal_result.passed,
            "direction": signal_result.direction,
            "block_reason": signal_result.block_reason or "",
            "buildup": signal_result.buildup or "",
            "price_chg_pct": signal_result.price_chg_pct,
            "oi_chg_pct": signal_result.oi_chg_pct,
            "pcr": signal_result.pcr,
            "pcr_bias": signal_result.pcr_bias or "",
            "conviction_score": signal_result.conviction_score,
            "atm_strike": signal_result.atm_strike,
            "feed_disagreement": signal_result.feed_disagreement,
        }
        file_exists = os.path.exists(self.path)
        with open(self.path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(row.keys()))
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
