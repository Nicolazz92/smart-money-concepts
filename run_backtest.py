#!/usr/bin/env python3
"""CLI entry point for backtesting the SMC strategy on CSV data.

Runs BacktestEngine with CsvProvider — no MT5 / OANDA / Telegram required.

Usage:
    python run_backtest.py
    python run_backtest.py --start 2024-03-01 --end 2026-07-01
    python run_backtest.py --bias-tf 1D --ob-tf 4H --no-killzones
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# Hide SMC credit banner
os.environ.setdefault("SMC_CREDIT", "0")

# Ensure project root on sys.path
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# IMPORTANT: import CsvProvider directly from its module, NOT from
# bot.data_providers package — that __init__.py imports MT5/OANDA providers
# which would require MetaTrader5 to be installed.
from bot.data_providers.csv_provider import CsvProvider  # noqa: E402
from bot.config import StrategyConfig  # noqa: E402
from bot.backtest.engine import BacktestEngine  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
# Backtest engine is verbose at DEBUG — keep INFO unless troubleshooting
logging.getLogger("bot.backtest.engine").setLevel(logging.INFO)
logging.getLogger("bot.strategy").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backtest the SNahary SMC strategy on CSV data."
    )
    p.add_argument("--data-dir", default="data", help="Folder with {SEC}_{TF}.csv")
    # Multi-symbol support. Either --symbols GAZP,SBER,... or --symbol XAUUSD.
    p.add_argument("--symbol", default=None,
                   help="Single symbol (e.g. XAUUSD). Mutually exclusive with --symbols.")
    p.add_argument("--symbols", default=None,
                   help="Comma-separated list (e.g. GAZP,SBER,GMKN,ROSN,LKOH,IMOEX)")
    # Preset shortcuts
    p.add_argument("--preset", choices=["gold", "moex"], default=None,
                   help="Use preset config: 'gold' = XAUUSD 1D/4H London/NY kz; "
                        "'moex' = 5 liquid MOEX shares + IMOEX 1D/60M MOEX main session.")
    p.add_argument(
        "--start", default=None,
        help="Backtest start date YYYY-MM-DD"
    )
    p.add_argument(
        "--end", default=None,
        help="Backtest end date YYYY-MM-DD"
    )
    # Timeframe overrides — default uses 1D bias + 4H OB which maximises the
    # yfinance 730-day intraday window. SNahary original is 4H/30M but 30M
    # only gives 60 days of yfinance data.
    p.add_argument("--bias-tf", default=None, help="Bias timeframe (e.g. 1D)")
    p.add_argument("--ob-tf", default=None, help="OB timeframe (e.g. 4H or 60M)")
    p.add_argument("--bias-swing", type=int, default=None)
    p.add_argument("--ob-swing", type=int, default=None)
    p.add_argument(
        "--sl-buffer-pips", type=float, default=None,
        help="SL buffer in pips (symbol pip-size from trade_calculator._PIP_SIZE)"
    )
    p.add_argument(
        "--no-killzones", action="store_true",
        help="Disable kill-zone time filter (trade any time)"
    )
    p.add_argument(
        "--min-rr", type=float, default=0.0,
        help="Drop signals with rr1 below this (e.g. 1.0 keeps only R:R >= 1)"
    )
    p.add_argument(
        "--cost-preset",
        choices=["zero", "freetrade", "strateg", "investor", "unified-daily"],
        default="zero",
        help="Finam cost model preset (default: zero = no costs, idealised)"
    )
    p.add_argument(
        "--out", default="",
        help="Output CSV path (default: data/backtest_signals_<ts>.csv)"
    )
    args = p.parse_args()
    apply_preset(args)
    return args


# Presets -----------------------------------------------------------------
PRESETS = {
    "gold": {
        "symbols": ["XAUUSD"],
        "start": "2024-03-01",
        "end": "2026-07-01",
        "bias_tf": "1D",
        "ob_tf": "4H",
        "bias_swing": 10,
        "ob_swing": 10,
        "sl_buffer_pips": 20.0,
        "killzones": [
            "London open kill zone",
            "New York kill zone",
            "London close kill zone",
        ],
    },
    "moex": {
        "symbols": ["GAZP", "SBER", "GMKN", "ROSN", "LKOH", "IMOEX"],
        "start": "2023-03-01",
        "end": "2026-07-01",
        "bias_tf": "1D",
        "ob_tf": "60M",
        "bias_swing": 10,
        "ob_swing": 10,
        # 50 pips * pip_size (0.01 for SBER/GAZP/ROSN/IMOEX = 0.5 RUB buffer;
        # 0.1 for GMKN/LKOH = 5 RUB buffer). Tunable per-ticker via --override.
        "sl_buffer_pips": 50.0,
        "killzones": ["MOEX main session"],
    },
}


def apply_preset(args: argparse.Namespace) -> None:
    """Apply preset defaults; CLI flags still take precedence."""
    if args.preset:
        preset = PRESETS[args.preset]
        # Symbols: CLI --symbols / --symbol wins
        if args.symbols:
            args._symbols = [s.strip() for s in args.symbols.split(",")]
        elif args.symbol:
            args._symbols = [args.symbol]
        else:
            args._symbols = preset["symbols"]
        # Fill remaining from preset only if not set on CLI
        args.start = args.start or preset["start"]
        args.end = args.end or preset["end"]
        args.bias_tf = args.bias_tf or preset["bias_tf"]
        args.ob_tf = args.ob_tf or preset["ob_tf"]
        args.bias_swing = args.bias_swing or preset["bias_swing"]
        args.ob_swing = args.ob_swing or preset["ob_swing"]
        args.sl_buffer_pips = args.sl_buffer_pips if args.sl_buffer_pips is not None else preset["sl_buffer_pips"]
        if not args.no_killzones:
            args._killzones = preset["killzones"]
        else:
            args._killzones = []
        return

    # No preset — backwards-compatible defaults (gold)
    if args.symbols:
        args._symbols = [s.strip() for s in args.symbols.split(",")]
    else:
        args._symbols = [args.symbol or "XAUUSD"]
    args.start = args.start or "2024-03-01"
    args.end = args.end or "2026-07-01"
    args.bias_tf = args.bias_tf or "1D"
    args.ob_tf = args.ob_tf or "4H"
    args.bias_swing = args.bias_swing or 10
    args.ob_swing = args.ob_swing or 10
    args.sl_buffer_pips = args.sl_buffer_pips if args.sl_buffer_pips is not None else 20.0
    args._killzones = [] if args.no_killzones else [
        "London open kill zone",
        "New York kill zone",
        "London close kill zone",
    ]


def build_strategy(args: argparse.Namespace) -> StrategyConfig:
    """Build a StrategyConfig from CLI args (post-preset)."""
    return StrategyConfig(
        pairs=args._symbols,
        bias_timeframe=args.bias_tf,
        ob_timeframe=args.ob_tf,
        bias_swing_length=args.bias_swing,
        ob_swing_length=args.ob_swing,
        sl_buffer_pips=args.sl_buffer_pips,
        killzones=args._killzones,
    )


def print_report(result, strategy: StrategyConfig, elapsed: float) -> None:
    """Pretty-print backtest summary to stdout."""
    print()
    print("=" * 70)
    print("BACKTEST RESULTS")
    print("=" * 70)
    print(f"Symbol:            {strategy.pairs}")
    print(f"Bias TF / OB TF:   {strategy.bias_timeframe} / {strategy.ob_timeframe}")
    print(f"Swing lengths:     bias={strategy.bias_swing_length}  ob={strategy.ob_swing_length}")
    print(f"SL buffer:         {strategy.sl_buffer_pips} pips")
    print(f"Kill zones:        {strategy.killzones or '(disabled)'}")
    print(f"Period:            {result.start_date} → {result.end_date}")
    print()

    print("─" * 70)
    print("TRADE OUTCOMES")
    print("─" * 70)
    print(f"  Total signals:        {result.total_signals}")
    print(f"  Wins (TP1 hit):       {result.wins}")
    print(f"  Losses (SL hit):      {result.losses}")
    print(f"  No TP target:         {result.no_tp}")
    print(f"  Expired (no hit):     {result.expired}")
    decided = result.wins + result.losses
    print(f"  Decided (W+L):        {decided}")
    print()

    if decided > 0:
        print("─" * 70)
        print("PERFORMANCE METRICS")
        print("─" * 70)
        print(f"  Win rate:             {result.win_rate:.1f}%")
        print(f"  Profit factor:        {result.profit_factor:.2f}")
        print(f"  Avg R (winners):      {result.avg_rr_winners:+.2f}R")
        print(f"  Total R:              {result.total_rr:+.2f}R")
        # Expectancy: average R per decided trade
        expectancy = result.total_rr / decided if decided > 0 else 0
        print(f"  Expectancy / trade:   {expectancy:+.3f}R")
        print()

    print("─" * 70)
    print("PIPELINE DIAGNOSTICS (where signals are lost)")
    print("─" * 70)
    print(f"  OBs detected (all):          {result.diag_total_obs_found}")
    print(f"  OBs in confluence w/ bias:   {result.diag_obs_in_confluence}")
    print(f"  Price returned to OB zone:   {result.diag_price_returns}")
    print(f"  Filtered out by kill zone:   {result.diag_filtered_by_kz}")
    print(f"  Filtered out by min R:R:     {result.diag_filtered_by_rr}")
    print(f"  Final signals recorded:      {result.total_signals}")
    print()

    print("─" * 70)
    print(f"Elapsed: {elapsed:.1f}s")
    print("=" * 70)


def export_signals(result, out_path: str | None) -> None:
    """Export all signals to CSV for post-hoc analysis."""
    if not result.signals:
        print("(no signals to export)")
        return
    rows = []
    for s in result.signals:
        rows.append({
            "signal_time": s.signal_time,
            "pair": s.pair,
            "direction": "BUY" if s.direction == 1 else "SELL",
            "bias_type": s.bias_type,
            "ob_time": s.ob_time,
            "entry": s.entry,
            "sl": s.sl,
            "tp1": s.tp1,
            "tp2": s.tp2,
            "rr1": s.rr1,
            "rr2": s.rr2,
            "ob_strength": s.ob_strength,
            "sl_pips": s.sl_pips,
            "outcome": s.outcome,
            "actual_rr": s.actual_rr,
            "exit_time": s.exit_time,
            "exit_price": s.exit_price,
        })
    import pandas as pd
    df = pd.DataFrame(rows)

    if not out_path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = f"data/backtest_signals_{ts}.csv"

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    print(f"\nSignals exported: {out_path}  ({len(df)} rows)")


def main() -> int:
    args = parse_args()
    print(f"Starting backtest: {len(args._symbols)} symbol(s) {args.bias_tf}/{args.ob_tf}")
    print(f"Symbols: {args._symbols}")
    print(f"Period: {args.start} → {args.end}")
    print(f"Min R:R filter:  {args.min_rr}")
    print(f"Cost model:      {args.cost_preset}")

    provider = CsvProvider(data_dir=args.data_dir)
    if not provider.connect():
        print("ERROR: CsvProvider failed to connect. Check data_dir.")
        return 1

    # Load Finam cost preset
    from bot.backtest.cost_model import FINAM_PRESETS
    cost_model = FINAM_PRESETS[args.cost_preset]
    print(f"Cost detail:     {cost_model.notes}")

    strategy = build_strategy(args)
    engine = BacktestEngine(
        provider=provider, strategy=strategy, notifier=None,
        min_rr=args.min_rr, cost_model=cost_model,
    )

    start_wall = datetime.now()
    result = engine.run(start_date=args.start, end_date=args.end)
    elapsed = (datetime.now() - start_wall).total_seconds()

    print_report(result, strategy, elapsed)
    export_signals(result, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
