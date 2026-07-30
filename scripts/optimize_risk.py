#!/usr/bin/env python3
"""Find optimal position size, max concurrent positions, and min_rr filter.

Tests all combinations and ranks by risk-adjusted return (return / max_drawdown).

Usage:
    python scripts/optimize_risk.py data/forts_1D4H_rr05_signals.csv
"""
from __future__ import annotations

import argparse
import sys
from itertools import product
from pathlib import Path

import pandas as pd


def simulate(signals: pd.DataFrame, position_pct: float, max_concurrent: int):
    """Simulate with given position size (% of equity per trade) and concurrency cap."""
    df = signals.dropna(subset=["actual_rr"]).copy()
    df["signal_time"] = pd.to_datetime(df["signal_time"], utc=True, format="ISO8601")
    if "exit_time" in df.columns:
        df["exit_time"] = pd.to_datetime(df["exit_time"], utc=True, format="ISO8601", errors="coerce")
    else:
        df["exit_time"] = pd.NaT
    mask_na = df["exit_time"].isna()
    if mask_na.any():
        fallback = df.loc[mask_na, "signal_time"] + pd.Timedelta(days=5)
        # Ensure same dtype (tz-aware UTC)
        df["exit_time"] = df["exit_time"].astype(object)
        df.loc[mask_na, "exit_time"] = fallback
    df = df.sort_values("signal_time").reset_index(drop=True)

    equity = 1.0
    open_pos = []
    events = []
    taken = 0
    skipped = 0

    pos_fraction = position_pct / 100.0  # e.g. 5% -> 0.05

    for _, r in df.iterrows():
        # Close matured
        still = []
        for p in open_pos:
            if p["exit_time"] <= r["signal_time"]:
                # P/L: position_fraction of equity × rr × risk_fraction_of_price
                # rr = |tp-entry|/|entry-sl|, risk_fraction = |entry-sl|/|entry|
                # Combined: rr × risk_fraction = |tp-entry|/|entry| = price_move_fraction
                price_move_fraction = abs(p["tp1"] - p["entry"]) / p["entry"]
                pnl = pos_fraction * p["rr"] * (abs(p["entry"] - p["sl"]) / p["entry"])
                equity *= (1 + pnl)
                events.append({"time": p["exit_time"], "pnl_pct": pnl * 100, "equity": equity})
            else:
                still.append(p)
        open_pos = still

        if len(open_pos) < max_concurrent:
            entry = float(r.get("entry", 0))
            sl = float(r.get("sl", 0))
            tp1 = float(r.get("tp1", 0) or 0)
            open_pos.append({
                "exit_time": r["exit_time"],
                "rr": float(r["actual_rr"]),
                "entry": entry,
                "sl": sl,
                "tp1": tp1,
            })
            taken += 1
        else:
            skipped += 1

    for p in open_pos:
        pnl = pos_fraction * p["rr"] * (abs(p["entry"] - p["sl"]) / p["entry"])
        equity *= (1 + pnl)
        events.append({"time": p["exit_time"], "pnl_pct": pnl * 100, "equity": equity})

    ev = pd.DataFrame(events)
    if ev.empty:
        return {"return_pct": 0, "max_dd_pct": 0, "mar": 0, "taken": 0, "skipped": skipped}

    # Max drawdown
    running_max = ev["equity"].cummax()
    drawdown = (ev["equity"] / running_max - 1) * 100
    max_dd = drawdown.min()

    ret_pct = (equity - 1) * 100
    mar = ret_pct / abs(max_dd) if max_dd < 0 else float("inf")

    return {
        "return_pct": round(ret_pct, 1),
        "max_dd_pct": round(max_dd, 1),
        "mar": round(mar, 2) if max_dd < 0 else 999.0,
        "taken": taken,
        "skipped": skipped,
        "final_equity": round(equity, 3),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("signals_csv", help="Signals CSV with actual_rr, signal_time, exit_time")
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    sig = pd.read_csv(args.signals_csv)

    # Filter options
    min_rr_options = [0.5, 1.0, 1.5]
    # Position size options (% of equity per trade)
    pos_options = [5, 10, 15, 20, 25, 30, 50, 100]
    # Max concurrent
    conc_options = [1, 2, 3, 5, 10, 999]

    sig["signal_time"] = pd.to_datetime(sig["signal_time"], utc=True, format="ISO8601")
    period_start = sig["signal_time"].min().strftime("%Y-%m-%d")
    period_end = sig["signal_time"].max().strftime("%Y-%m-%d")

    print("=" * 95)
    if args.label:
        print(f"  {args.label}")
    print(f"  Period: {period_start} → {period_end}")
    print(f"  Total signals: {len(sig)}")
    print("=" * 95)

    results = []
    for min_rr, pos_pct, max_conc in product(min_rr_options, pos_options, conc_options):
        # Filter signals by min_rr
        filtered = sig.copy()
        if "rr1" in filtered.columns:
            filtered = filtered[filtered["rr1"].fillna(0) >= min_rr]
        if len(filtered) == 0 or filtered["actual_rr"].isna().all():
            continue

        res = simulate(filtered, pos_pct, max_conc if max_conc < 999 else 9999)
        results.append({
            "min_rr": min_rr,
            "pos_pct": pos_pct,
            "max_conc": max_conc if max_conc < 999 else "unlimited",
            **res,
        })

    df = pd.DataFrame(results)

    # Rank by MAR (return / max_drawdown) — risk-adjusted
    df = df.sort_values("mar", ascending=False)

    print(f"\n{'min_rr':>6} {'pos%':>5} {'max_conc':>9} {'Return%':>9} {'MaxDD%':>8} {'MAR':>7} {'Taken':>6} {'Skip':>6} {'Equity':>8}")
    print("-" * 80)
    for _, r in df.head(30).iterrows():
        print(f"{r['min_rr']:>6} {r['pos_pct']:>5} {str(r['max_conc']):>9} "
              f"{r['return_pct']:>+9.1f} {r['max_dd_pct']:>+8.1f} {r['mar']:>7.2f} "
              f"{r['taken']:>6} {r['skipped']:>6} {r['final_equity']:>8}")

    # Also show best by raw return
    print(f"\n--- TOP 10 by raw return ---")
    df_ret = df.sort_values("return_pct", ascending=False).head(10)
    for _, r in df_ret.iterrows():
        print(f"{r['min_rr']:>6} {r['pos_pct']:>5} {str(r['max_conc']):>9} "
              f"{r['return_pct']:>+9.1f} {r['max_dd_pct']:>+8.1f} {r['mar']:>7.2f} "
              f"{r['taken']:>6} {r['skipped']:>6} {r['final_equity']:>8}")

    # Sweet spot: max DD < 20% and return > 100%
    print(f"\n--- SWEET SPOT (MaxDD < 20%, Return > 50%) ---")
    sweet = df[(df["max_dd_pct"] > -20) & (df["return_pct"] > 50)].sort_values("mar", ascending=False)
    if sweet.empty:
        sweet = df[(df["max_dd_pct"] > -30) & (df["return_pct"] > 30)].sort_values("mar", ascending=False)
    for _, r in sweet.head(10).iterrows():
        print(f"min_rr={r['min_rr']} pos={r['pos_pct']}% conc={r['max_conc']} → "
              f"Return={r['return_pct']:+.1f}% MaxDD={r['max_dd_pct']:.1f}% MAR={r['mar']:.2f} "
              f"(taken={r['taken']}, skip={r['skipped']})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
