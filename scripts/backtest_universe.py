#!/usr/bin/env python3
"""Run backtest across all tickers in a list file and report per-ticker stats.

Designed for the "expand to top-100 TQBR" experiment. Reads a JSON list of
SECIDs (same format as download_moex.py --list), runs the SMC backtest on
each, and produces:

  1. Console table sorted by total R (best → worst)
  2. data/universe_report.csv  — full per-ticker breakdown
  3. data/universe_signals.csv — every signal (for post-hoc analysis)

Usage:
    python scripts/backtest_universe.py --list data/_top100_tqbr_clean.json
    python scripts/backtest_universe.py --list data/_top100_tqbr_clean.json \
        --min-rr 1.0 --cost-preset strateg
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("SMC_CREDIT", "0")
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.data_providers.csv_provider import CsvProvider  # noqa: E402
from bot.config import StrategyConfig  # noqa: E402
from bot.backtest.engine import BacktestEngine  # noqa: E402
from bot.backtest.cost_model import FINAM_PRESETS  # noqa: E402

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("bot.backtest.engine").setLevel(logging.WARNING)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", required=True, help="JSON array of SECIDs "
                     "(MOEX) OR array of {symbol,category,earliest} (Bybit)")
    ap.add_argument("--venue", choices=["moex", "bybit"], default="moex",
                    help="Source venue: picks filename suffix + default cost")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default="2026-07-18")
    ap.add_argument("--bias-tf", default="1D")
    ap.add_argument("--ob-tf", default="60M")
    ap.add_argument("--bias-swing", type=int, default=10)
    ap.add_argument("--ob-swing", type=int, default=10)
    ap.add_argument("--sl-buffer-pips", type=float, default=None)
    ap.add_argument("--no-killzones", action="store_true")
    ap.add_argument("--killzone", default="MOEX main session",
                    help="Killzone name (default: MOEX main session). "
                         "Use --no-killzones for crypto (24/7 market).")
    ap.add_argument("--min-rr", type=float, default=1.0)
    ap.add_argument("--cost-preset", default=None,
                    choices=list(FINAM_PRESETS.keys()))
    ap.add_argument("--out-report", default="data/universe_report.csv")
    ap.add_argument("--out-signals", default="data/universe_signals.csv")
    args = ap.parse_args()

    # Venue-specific defaults
    if args.venue == "bybit":
        suffix = "_bybit"
        if args.start is None: args.start = "2023-10-25"
        if args.cost_preset is None: args.cost_preset = "bybit-perp"
        if args.sl_buffer_pips is None: args.sl_buffer_pips = 200.0  # crypto pips (0.01 size = 2.0 price)
        if not args.no_killzones:
            # Crypto trades 24/7 — killzones (MOEX/London/NY) don't apply
            args.no_killzones = True
    else:
        suffix = ""
        if args.start is None: args.start = "2023-03-01"
        if args.cost_preset is None: args.cost_preset = "strateg"
        if args.sl_buffer_pips is None: args.sl_buffer_pips = 50.0

    # Parse list — supports both formats
    raw = json.loads(Path(args.list).read_text(encoding="utf-8"))
    if raw and isinstance(raw[0], dict):
        secids = [item["symbol"] for item in raw]
    else:
        secids = list(raw)

    # Filter to those actually downloaded
    secids = [s for s in secids
              if (Path(args.data_dir) / f"{s}{suffix}_{args.bias_tf}.csv").exists()]
    print(f"Backtesting {len(secids)} instruments from {args.list}  (venue={args.venue})")
    print(f"Period: {args.start} → {args.end}  TF: {args.bias_tf}/{args.ob_tf}")
    print(f"min_rr={args.min_rr}  cost={args.cost_preset}  "
          f"killzones={'(none)' if args.no_killzones else [args.killzone]}")
    print()

    provider = CsvProvider(data_dir=args.data_dir, filename_suffix=suffix)
    provider.connect()
    cost_model = FINAM_PRESETS[args.cost_preset]
    killzones = [] if args.no_killzones else [args.killzone]

    rows = []
    all_signals_rows = []
    t0 = time.time()
    for i, sec in enumerate(secids, 1):
        # Skip tickers missing intraday data
        if not (Path(args.data_dir) / f"{sec}{suffix}_{args.ob_tf}.csv").exists():
            print(f"[{i}/{len(secids)}] {sec}: SKIP (no {args.ob_tf})")
            continue
        strategy = StrategyConfig(
            pairs=[sec],
            bias_timeframe=args.bias_tf,
            ob_timeframe=args.ob_tf,
            bias_swing_length=args.bias_swing,
            ob_swing_length=args.ob_swing,
            sl_buffer_pips=args.sl_buffer_pips,
            killzones=killzones,
        )
        try:
            engine = BacktestEngine(
                provider=provider, strategy=strategy,
                notifier=None, min_rr=args.min_rr, cost_model=cost_model,
            )
            res = engine.run(start_date=args.start, end_date=args.end)
        except Exception as e:
            print(f"[{i}/{len(secids)}] {sec}: ERROR {e}")
            rows.append({
                "ticker": sec, "signals": 0, "wins": 0, "losses": 0,
                "win_rate": 0, "profit_factor": 0, "total_rr": 0,
                "avg_rr_winners": 0, "expectancy": 0, "status": f"ERROR:{type(e).__name__}",
            })
            continue

        decided = res.wins + res.losses
        expectancy = (res.total_rr / decided) if decided > 0 else 0
        rows.append({
            "ticker": sec,
            "signals": res.total_signals,
            "wins": res.wins,
            "losses": res.losses,
            "win_rate": round(res.win_rate, 1),
            "profit_factor": round(res.profit_factor, 2) if res.profit_factor != float("inf") else 999,
            "total_rr": round(res.total_rr, 1),
            "avg_rr_winners": round(res.avg_rr_winners, 2),
            "expectancy": round(expectancy, 3),
            "status": "OK",
        })
        for s in res.signals:
            all_signals_rows.append({
                "ticker": s.pair,
                "signal_time": s.signal_time,
                "direction": "BUY" if s.direction == 1 else "SELL",
                "entry": s.entry, "sl": s.sl, "tp1": s.tp1, "rr1": s.rr1,
                "outcome": s.outcome, "actual_rr": s.actual_rr,
            })
        flag = "✓" if res.total_rr > 0 else "✗"
        print(f"[{i}/{len(secids)}] {sec:8}  sig={res.total_signals:>3}  "
              f"W/L={res.wins}/{res.losses}  WR={res.win_rate:>4.0f}%  "
              f"PF={min(res.profit_factor,99):>5.2f}  R={res.total_rr:>+7.1f}  {flag}")

    elapsed = time.time() - t0

    # ─── Aggregate report ───
    import pandas as pd
    df = pd.DataFrame(rows).sort_values("total_rr", ascending=False)
    df.to_csv(args.out_report, index=False)
    pd.DataFrame(all_signals_rows).to_csv(args.out_signals, index=False)

    # ─── Summary table to console ───
    print()
    print("=" * 90)
    print(f"UNIVERSE BACKTEST  —  {len(df)} tickers  ({args.start} → {args.end})")
    print("=" * 90)
    print(df.to_string(index=False))
    print()
    decided_tk = df[(df["wins"] + df["losses"]) > 0]
    print("─" * 90)
    print("AGGREGATE")
    print("─" * 90)
    print(f"  Tickers tested (with signals):   {len(decided_tk)}/{len(df)}")
    print(f"  Total signals:                   {int(df['signals'].sum())}")
    print(f"  Total wins / losses:             {int(df['wins'].sum())}/{int(df['losses'].sum())}")
    if (df["wins"] + df["losses"]).sum() > 0:
        agg_wr = df["wins"].sum() / (df["wins"] + df["losses"]).sum() * 100
        print(f"  Aggregate win rate:              {agg_wr:.1f}%")
    print(f"  Total R (sum across tickers):    {df['total_rr'].sum():+.1f}")
    print(f"  Tickers with positive R:         {(df['total_rr']>0).sum()}")
    print(f"  Tickers with negative R:         {(df['total_rr']<0).sum()}")
    print(f"  Tickers with 0 signals:          {(df['signals']==0).sum()}")
    print(f"  Elapsed: {elapsed:.1f}s")
    print()
    print(f"Reports: {args.out_report}")
    print(f"Signals: {args.out_signals}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
