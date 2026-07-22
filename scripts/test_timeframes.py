#!/usr/bin/env python3
"""Test SMC strategy on different timeframe combinations on XAUUSD.

We have all 4 timeframes for gold (30M, 1H, 4H, 1D). Test these setups:
  - 1D + 60M  (current baseline)    - intraday swing
  - 1D + 4H   (less noise)          - swing
  - 1W + 1D   (position trading)    - weekly
  - 4H + 30M  (swing-scalp)         - intraday aggressive

For each, run a fixed-parameter backtest (author defaults) and a quick WF.

Usage:
    python scripts/test_timeframes.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pandas as pd

os.environ.setdefault("SMC_CREDIT", "0")
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.data_providers.csv_provider import CsvProvider  # noqa: E402
from bot.config import StrategyConfig  # noqa: E402
from bot.backtest.engine import BacktestEngine  # noqa: E402
from bot.backtest.cost_model import FINAM_PRESETS  # noqa: E402


# Timeframe combinations to test (bias_TF, ob_TF, label)
TF_COMBOS = [
    ("1D", "60M", "1D+60M (current)"),
    ("1D", "4H",  "1D+4H (swing)"),
    ("1W", "1D",  "1W+1D (position)"),
    ("4H", "30M", "4H+30M (scalp)"),
]


def resample_to_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """Resample daily data to weekly (W-FRI) for 1W+1D setup."""
    return df.resample("W-FRI").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()


def run_single_backtest(provider, bias_tf, ob_tf, symbol, start, end,
                        sl_buffer, cost_preset, killzones=None):
    """Run one backtest with author defaults."""
    strategy = StrategyConfig(
        pairs=[symbol],
        bias_timeframe=bias_tf,
        ob_timeframe=ob_tf,
        bias_swing_length=10,
        ob_swing_length=10,
        sl_buffer_pips=sl_buffer,
        killzones=killzones or [],
    )
    engine = BacktestEngine(
        provider=provider, strategy=strategy, notifier=None,
        min_rr=1.0, cost_model=FINAM_PRESETS[cost_preset],
    )
    return engine.run(start_date=start, end_date=end)


def main() -> int:
    print("=" * 80)
    print("TIMEFRAME COMBINATION TEST  (XAUUSD)")
    print("=" * 80)
    print()

    # We need XAUUSD with custom suffix-less provider (existing files).
    # XAUUSD_1D.csv, XAUUSD_4H.csv, XAUUSD_60M = XAUUSD_1H, XAUUSD_30M
    # Strategy expects file naming {SYMBOL}_{TF}.csv — for "60M" we have 1H.
    # Quick fix: load XAUUSD with explicit mapping.

    out_rows = []

    for bias_tf, ob_tf, label in TF_COMBOS:
        print(f"--- {label} ---")

        # Build a small ad-hoc provider for this TF combo.
        # We need both {SYMBOL}_{bias_tf}.csv and {SYMBOL}_{ob_tf}.csv.
        # 1W doesn't exist → resample from 1D on the fly.
        if bias_tf == "1W":
            # Create weekly file from daily
            daily = pd.read_csv("data/XAUUSD_1D.csv", parse_dates=["time"]).set_index("time")
            weekly = resample_to_weekly(daily)
            weekly.to_csv("data/XAUUSD_1W.csv")
            print(f"  (generated 1W from 1D: {len(weekly)} bars)")

        # For ob_tf=60M we use XAUUSD_1H (no 60M file but it's the same)
        ob_tf_file = ob_tf
        if ob_tf == "60M" and not Path("data/XAUUSD_60M.csv").exists():
            # Copy 1H to 60M naming
            if not Path("data/XAUUSD_60M.csv").exists():
                Path("data/XAUUSD_60M.csv").write_bytes(
                    Path("data/XAUUSD_1H.csv").read_bytes())
                print(f"  (created XAUUSD_60M.csv from XAUUSD_1H.csv)")

        try:
            provider = CsvProvider(data_dir="data")
            provider.connect()
            res = run_single_backtest(
                provider, bias_tf, ob_tf, "XAUUSD",
                start="2024-03-01", end="2026-07-18",
                sl_buffer=20.0,
                cost_preset="zero",  # gold has no proper cost model yet
                killzones=[],  # gold: 24h market, no MOEX killzone
            )
            decided = res.wins + res.losses
            exp = res.total_rr / decided if decided > 0 else 0
            out_rows.append({
                "combo": label,
                "bias_tf": bias_tf,
                "ob_tf": ob_tf,
                "signals": res.total_signals,
                "wins": res.wins,
                "losses": res.losses,
                "win_rate": round(res.win_rate, 1),
                "total_rr": round(res.total_rr, 2),
                "expectancy": round(exp, 4),
                "profit_factor": round(res.profit_factor, 2) if res.profit_factor != float("inf") else 999,
            })
            print(f"  signals={res.total_signals}  W/L={res.wins}/{res.losses}  "
                  f"WR={res.win_rate:.1f}%  R={res.total_rr:+.2f}  "
                  f"exp={exp:+.3f}  PF={min(res.profit_factor,99):.2f}")
        except Exception as e:
            print(f"  ERROR: {e}")
            out_rows.append({
                "combo": label, "bias_tf": bias_tf, "ob_tf": ob_tf,
                "signals": 0, "wins": 0, "losses": 0,
                "win_rate": 0, "total_rr": 0, "expectancy": 0,
                "profit_factor": 0, "error": str(e),
            })
        print()

    # ─── Summary table ───
    print("=" * 80)
    print("SUMMARY: TIMEFRAME COMBINATIONS on XAUUSD (zero cost)")
    print("=" * 80)
    df = pd.DataFrame(out_rows)
    print(df.to_string(index=False))
    df.to_csv("data/timeframe_test_xauusd.csv", index=False)
    print(f"\nSaved: data/timeframe_test_xauusd.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
