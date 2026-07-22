#!/usr/bin/env python3
"""Detailed monthly breakdown for a signals CSV.

For each month shows: # signals, # decided, WR, total R, expectancy, % return
(assuming 1% risk per trade, compounding, max 5 concurrent positions).

Usage:
    python scripts/detailed_monthly.py data/wf_bybit_oos1m_4H_signals.csv --label "Bybit WF 1m 4H"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd


def simulate_with_dates(signals: pd.DataFrame, risk_pct: float, max_concurrent: int):
    """Walk signals chronologically, simulate positions with concurrency cap.
    Returns DataFrame of closed positions with their P/L % and dates.
    """
    df = signals.dropna(subset=["actual_rr"]).copy()
    df["signal_time"] = pd.to_datetime(df["signal_time"], utc=True, format="ISO8601")
    if "exit_time" in df.columns:
        df["exit_time"] = pd.to_datetime(df["exit_time"], utc=True, format="ISO8601", errors="coerce")
    else:
        df["exit_time"] = pd.NaT
    # If exit_time missing, assume 5-day hold
    mask_na = df["exit_time"].isna()
    if mask_na.all():
        df["exit_time"] = df["signal_time"] + pd.Timedelta(days=5)
    else:
        df.loc[mask_na, "exit_time"] = df.loc[mask_na, "signal_time"] + pd.Timedelta(days=5)

    df = df.sort_values("signal_time").reset_index(drop=True)

    equity = 1.0
    open_pos = []
    events = []
    skipped = 0
    taken = 0

    for _, r in df.iterrows():
        # Close matured positions
        still = []
        for p in open_pos:
            if p["exit_time"] <= r["signal_time"]:
                pnl_pct = risk_pct * p["rr"]
                equity *= (1 + pnl_pct)
                events.append({
                    "exit_time": p["exit_time"],
                    "ticker": p.get("ticker", ""),
                    "rr": p["rr"],
                    "pnl_pct": pnl_pct * 100,
                    "equity_after": equity,
                })
            else:
                still.append(p)
        open_pos = still

        if len(open_pos) < max_concurrent:
            open_pos.append({
                "exit_time": r["exit_time"],
                "rr": float(r["actual_rr"]),
                "ticker": r.get("ticker", ""),
            })
            taken += 1
        else:
            skipped += 1

    # Close remaining
    for p in open_pos:
        pnl_pct = risk_pct * p["rr"]
        equity *= (1 + pnl_pct)
        events.append({
            "exit_time": p["exit_time"],
            "ticker": p.get("ticker", ""),
            "rr": p["rr"],
            "pnl_pct": pnl_pct * 100,
            "equity_after": equity,
        })

    return pd.DataFrame(events), {"taken": taken, "skipped": skipped, "final_equity": equity}


def monthly_table(events: pd.DataFrame, signals: pd.DataFrame):
    """Combine signal counts + realised P/L per month."""
    sig = signals.dropna(subset=["actual_rr"]).copy()
    sig["time"] = pd.to_datetime(sig["signal_time"], utc=True, format="ISO8601")
    sig["ym"] = sig["time"].dt.strftime("%Y-%m")

    ev = events.copy()
    ev["time"] = pd.to_datetime(ev["exit_time"], utc=True, format="ISO8601")
    ev["ym"] = ev["time"].dt.strftime("%Y-%m")

    rows = []
    all_months = sorted(set(sig["ym"].unique()) | set(ev["ym"].unique()))
    for ym in all_months:
        sg = sig[sig["ym"] == ym]
        eg = ev[ev["ym"] == ym]
        wins_sig = (sg["outcome"] == "WIN_TP1").sum()
        losses_sig = (sg["outcome"] == "LOSS").sum()
        decided = wins_sig + losses_sig
        wr = wins_sig / decided * 100 if decided > 0 else 0
        # Period return from realised P/L (sum of pnl_pct / 100, compounded within month)
        period_ret = 1.0
        for pnl in eg["pnl_pct"]:
            period_ret *= (1 + pnl / 100)
        ret_pct = (period_ret - 1) * 100
        rows.append({
            "month": ym,
            "signals": int(len(sg)),
            "wins": int(wins_sig),
            "losses": int(losses_sig),
            "win_rate": round(wr, 1),
            "total_rr": round(sg["actual_rr"].sum(), 2),
            "return_pct": round(ret_pct, 2),
            "closes": int(len(eg)),
        })
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("signals_csv")
    ap.add_argument("--label", default="", help="Run label for header")
    ap.add_argument("--risk", type=float, default=0.01)
    ap.add_argument("--max-concurrent", type=int, default=5)
    args = ap.parse_args()

    sig = pd.read_csv(args.signals_csv)
    if "ticker" not in sig.columns:
        sig["ticker"] = sig.get("pair", "?")

    n_decided = sig["actual_rr"].notna().sum()
    sig["time"] = pd.to_datetime(sig["signal_time"], utc=True, format="ISO8601")
    period_start = sig["time"].min().strftime("%Y-%m-%d")
    period_end = sig["time"].max().strftime("%Y-%m-%d")

    print("=" * 92)
    if args.label:
        print(f"  {args.label}")
        print("  " + "-" * 88)
    print(f"  Period: {period_start} → {period_end}  ({n_decided} decided signals)")
    print(f"  Risk model: {args.risk*100:.1f}% per trade, max {args.max_concurrent} concurrent, compounding")
    print("=" * 92)

    events, stats = simulate_with_dates(sig, args.risk, args.max_concurrent)
    monthly = monthly_table(events, sig)

    # Print monthly table
    print(f"\n{'Month':<8} {'Signals':>8} {'W':>4} {'L':>4} {'WR%':>6} {'R':>8} {'Return%':>9} {'Closes':>7}")
    print("-" * 60)
    for _, r in monthly.iterrows():
        print(f"{r['month']:<8} {r['signals']:>8} {r['wins']:>4} {r['losses']:>4} "
              f"{r['win_rate']:>5.1f}% {r['total_rr']:>+8.2f} {r['return_pct']:>+8.2f}% {r['closes']:>7}")

    # Summary stats
    print("-" * 60)
    total_signals = monthly["signals"].sum()
    total_wins = monthly["wins"].sum()
    total_losses = monthly["losses"].sum()
    total_wr = total_wins / max(total_wins + total_losses, 1) * 100
    total_rr = monthly["total_rr"].sum()
    final_ret = (stats["final_equity"] - 1) * 100
    n_pos = (monthly["return_pct"] > 0).sum()
    n_neg = (monthly["return_pct"] < 0).sum()
    n_months = len(monthly)

    print(f"{'TOTAL':<8} {total_signals:>8} {total_wins:>4} {total_losses:>4} "
          f"{total_wr:>5.1f}% {total_rr:>+8.2f} {final_ret:>+8.2f}%")
    print()
    print(f"  Months: {n_months}  (positive: {n_pos}, negative: {n_neg})")
    print(f"  Avg month: {monthly['return_pct'].mean():+.2f}%, median: {monthly['return_pct'].median():+.2f}%")
    print(f"  Best month: {monthly['return_pct'].max():+.2f}% ({monthly.loc[monthly['return_pct'].idxmax(), 'month']})")
    print(f"  Worst month: {monthly['return_pct'].min():+.2f}% ({monthly.loc[monthly['return_pct'].idxmin(), 'month']})")
    print(f"  Trades taken: {stats['taken']}, skipped (concurrency): {stats['skipped']}")
    print(f"  Final equity: {stats['final_equity']:.4f}x  (return: {final_ret:+.2f}%)")


if __name__ == "__main__":
    main()
