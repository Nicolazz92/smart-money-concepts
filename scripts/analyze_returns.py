#!/usr/bin/env python3
"""Convert backtest signals into realistic calendar returns (% per month/quarter/year).

Portfolio simulation with concurrency cap:
  - Walk through signals chronologically
  - At each signal, if # currently-open positions < max_concurrent → take it
    (risk risk_pct of CURRENT equity on this position)
  - When a position's exit_time arrives, realise its P/L into equity
  - If at cap, skip the signal (realistic — limited capital)

This is closer to real trading than naive "1% per trade" which assumes
unlimited simultaneous positions.

Risk model:
  - risk_pct = 1.0% of CURRENT equity risked per trade (compounding)
  - actual P/L on equity = risk_pct × actual_rr

Outputs:
  - Monthly / Quarterly / Yearly returns table
  - Equity curve CSV
  - Max drawdown, CAGR
  - Concurrent position statistics

Usage:
    python scripts/analyze_returns.py data/bybit_universe_signals.csv
    python scripts/analyze_returns.py data/moex_universe_signals.csv --max-concurrent 5
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd


def simulate_portfolio(
    signals: pd.DataFrame,
    risk_pct: float,
    max_concurrent: int,
    compound: bool = True,
) -> tuple[pd.DataFrame, dict]:
    """Walk signals in time, open positions respecting max_concurrent.

    Returns (events_df, stats) where events_df has one row per realisation.
    """
    df = signals.dropna(subset=["actual_rr"]).copy()
    # Need signal_time AND exit_time for concurrency tracking.
    # The signals CSV from backtest_universe.py has signal_time but not exit_time.
    # We approximate hold period from rr direction: assume average hold = 5 bars
    # of OB timeframe (60M = 5 hours ≈ 0.21 day; 1D = 5 days).
    # Better: derive hold from the underlying signal CSV with exit_time.
    df["signal_time"] = pd.to_datetime(df["signal_time"], utc=True)
    df["exit_time"] = pd.to_datetime(df.get("exit_time", pd.NaT), utc=True,
                                      errors="coerce")
    # Fallback: if exit_time missing, approximate hold by RR magnitude
    # (winners tend to exit faster at TP1; both bounded by data end).
    if df["exit_time"].isna().all():
        # Use 5 days as default hold
        df["exit_time"] = df["signal_time"] + pd.Timedelta(days=5)

    df = df.sort_values("signal_time").reset_index(drop=True)

    equity = 1.0
    initial = 1.0
    open_positions: list[dict] = []  # [{exit_time, rr}]
    events = []
    skipped = 0
    taken = 0
    max_open_seen = 0
    sum_open = 0
    n_checks = 0

    for _, r in df.iterrows():
        # First, close any positions whose exit_time has passed
        still_open = []
        for pos in open_positions:
            if pos["exit_time"] <= r["signal_time"]:
                # Realise P/L
                base = equity if compound else initial
                pnl_pct = risk_pct * pos["rr"]
                equity = equity * (1 + pnl_pct) if compound else equity + pnl_pct
                events.append({
                    "time": pos["exit_time"],
                    "ticker": pos.get("ticker", ""),
                    "rr": pos["rr"],
                    "pnl_pct": pnl_pct * 100,
                    "equity": equity,
                    "action": "CLOSE",
                })
            else:
                still_open.append(pos)
        open_positions = still_open

        # Try to open new position
        n_checks += 1
        sum_open += len(open_positions)
        max_open_seen = max(max_open_seen, len(open_positions))
        if len(open_positions) < max_concurrent:
            open_positions.append({
                "exit_time": r["exit_time"],
                "rr": float(r["actual_rr"]),
                "ticker": r.get("ticker", ""),
            })
            taken += 1
            events.append({
                "time": r["signal_time"],
                "ticker": r.get("ticker", ""),
                "rr": float(r["actual_rr"]),
                "pnl_pct": 0.0,  # realised at close
                "equity": equity,
                "action": "OPEN",
            })
        else:
            skipped += 1

    # Close any remaining at end
    for pos in open_positions:
        base = equity if compound else initial
        pnl_pct = risk_pct * pos["rr"]
        equity = equity * (1 + pnl_pct) if compound else equity + pnl_pct
        events.append({
            "time": pos["exit_time"],
            "ticker": pos.get("ticker", ""),
            "rr": pos["rr"],
            "pnl_pct": pnl_pct * 100,
            "equity": equity,
            "action": "CLOSE",
        })

    stats = {
        "signals_total": len(df),
        "taken": taken,
        "skipped_concurrency": skipped,
        "max_concurrent_seen": max_open_seen,
        "avg_concurrent": sum_open / max(n_checks, 1),
        "final_equity": equity,
    }
    events_df = pd.DataFrame(events)
    if not events_df.empty:
        # Only CLOSE events affect equity; keep those for return analysis
        events_df = events_df[events_df["action"] == "CLOSE"].reset_index(drop=True)
    return events_df, stats


def period_returns(events_df: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Group realised P/L by calendar period."""
    if events_df.empty:
        return pd.DataFrame()
    df = events_df.copy()
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time").sort_index()
    df["period"] = df.index.tz_convert(None).to_period(freq)
    out = []
    for period, group in df.groupby("period"):
        # Period return: product of (1 + pnl_pct/100) - 1
        period_ret = 1.0
        for pnl in group["pnl_pct"]:
            period_ret *= (1 + pnl / 100)
        ret_pct = (period_ret - 1) * 100
        out.append({
            "period": str(period),
            "n_closes": len(group),
            "return_pct": ret_pct,
        })
    return pd.DataFrame(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("signals_csv")
    ap.add_argument("--risk", type=float, default=0.01,
                    help="Risk per trade as fraction of equity (default 0.01 = 1%)")
    ap.add_argument("--max-concurrent", type=int, default=5,
                    help="Max simultaneous open positions (default 5)")
    ap.add_argument("--no-compound", action="store_true")
    ap.add_argument("--out-equity", default=None)
    args = ap.parse_args()

    sig = pd.read_csv(args.signals_csv)
    if "actual_rr" not in sig.columns:
        print("ERROR: signals file missing 'actual_rr' column")
        return 1
    print(f"Loaded {len(sig)} signals from {args.signals_csv}")
    print(f"Risk model: {args.risk*100:.2f}% per trade, "
          f"max {args.max_concurrent} concurrent, "
          f"{'compounding' if not args.no_compound else 'fixed-fractional'}")
    print()

    events, stats = simulate_portfolio(sig, args.risk, args.max_concurrent,
                                        compound=not args.no_compound)
    if events.empty:
        print("No trades realised.")
        return 0

    final_equity = stats["final_equity"]
    total_return = (final_equity - 1) * 100
    n_closes = len(events)

    # Max drawdown on equity column
    events_sorted = events.sort_values("time").reset_index(drop=True)
    running_max = events_sorted["equity"].cummax()
    drawdown = (events_sorted["equity"] / running_max - 1) * 100
    max_dd = drawdown.min()

    span_seconds = (events_sorted["time"].iloc[-1] - events_sorted["time"].iloc[0]).total_seconds()
    span_years = span_seconds / (365.25 * 86400)

    if total_return > -100 and span_years > 0:
        cagr = (final_equity ** (1 / span_years) - 1) * 100
    else:
        cagr = float("nan")

    # ── Summary
    print("=" * 78)
    print("PORTFOLIO SIMULATION SUMMARY")
    print("=" * 78)
    print(f"  Signals generated:           {stats['signals_total']}")
    print(f"  Trades taken:                {stats['taken']}  "
          f"({stats['taken']/stats['signals_total']*100:.0f}% of signals)")
    print(f"  Skipped (concurrency cap):   {stats['skipped_concurrency']}")
    print(f"  Max concurrent seen:         {stats['max_concurrent_seen']}")
    print(f"  Avg concurrent at signal:    {stats['avg_concurrent']:.2f}")
    print(f"  Span:                        {span_years:.2f} years")
    print(f"  Total return:                {total_return:+.2f}%")
    print(f"  Final equity:                {final_equity:.4f}x  (from 1.0)")
    print(f"  CAGR:                        {cagr:+.2f}%/year")
    print(f"  Max drawdown:                {max_dd:.2f}%")
    print()

    # ── Period returns
    for freq, name in [("M", "MONTHLY"), ("Q", "QUARTERLY"), ("Y", "YEARLY")]:
        pr = period_returns(events, freq)
        if pr.empty:
            continue
        print("─" * 78)
        print(f"{name} RETURNS  ({len(pr)} periods)")
        print("─" * 78)
        with pd.option_context("display.max_rows", None, "display.width", 120):
            print(pr.to_string(index=False))
        rets = pr["return_pct"]
        pos = (rets > 0).sum()
        neg = (rets < 0).sum()
        print()
        print(f"  Positive periods: {pos}/{len(rets)}  ({pos/len(rets)*100:.0f}%)")
        print(f"  Avg return:       {rets.mean():+.2f}%")
        print(f"  Median return:    {rets.median():+.2f}%")
        print(f"  Best period:      {rets.max():+.2f}%")
        print(f"  Worst period:     {rets.min():+.2f}%")
        print()

    out_eq = args.out_equity or str(Path(args.signals_csv).with_suffix("")) + "_equity.csv"
    events_sorted.to_csv(out_eq, index=False)
    print(f"Equity curve saved: {out_eq}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
