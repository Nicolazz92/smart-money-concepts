#!/usr/bin/env python3
"""Analyze backtest signals under the "full deposit, no leverage" risk model.

Risk model:
  LONG  — open at 100% of free cash (no leverage). Single position per deposit.
  SHORT — open at 50% of free cash. Can stack 2 shorts simultaneously.
  When open positions close, cash is freed for new entries.

P/L per trade:
  LONG  WIN:  pnl% = (tp1 - entry) / entry × 100         [position = 100% of entry-time cash]
  LONG  LOSS: pnl% = (sl  - entry) / entry × 100         [negative]
  SHORT WIN:  pnl% = (entry - tp1) / entry × 100 × 0.5   [position = 50%]
  SHORT LOSS: pnl% = (entry - sl)  / entry × 100 × 0.5   [negative, since sl > entry for short]

Note: this model is much more aggressive than 1%-risk. Drawdowns will be larger.

Usage:
    python scripts/analyze_full_deposit.py data/wf_bybit_oos1m_signals.csv --label "Bybit 1m 60M"
    python scripts/analyze_full_deposit.py data/wf_moex_oos3m_signals.csv --label "MOEX 3m 60M"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd


def simulate_full_deposit(signals: pd.DataFrame):
    """Walk signals chronologically. Apply full-deposit risk model.

    Returns (events_df, stats) where events_df has one row per realized close.
    """
    df = signals.dropna(subset=["actual_rr"]).copy()
    df["signal_time"] = pd.to_datetime(df["signal_time"], utc=True, format="ISO8601")
    if "exit_time" in df.columns:
        df["exit_time"] = pd.to_datetime(df["exit_time"], utc=True, format="ISO8801", errors="coerce")
    else:
        df["exit_time"] = pd.NaT
    mask_na = df["exit_time"].isna()
    if mask_na.all():
        df["exit_time"] = df["signal_time"] + pd.Timedelta(days=5)
    else:
        df.loc[mask_na, "exit_time"] = df.loc[mask_na, "signal_time"] + pd.Timedelta(days=5)

    df = df.sort_values("signal_time").reset_index(drop=True)

    equity = 1.0  # start with 1.0 (100%)
    free_cash = 1.0  # initially all free
    open_positions = []  # list of dicts
    events = []
    skipped_no_cash = 0
    taken = 0

    def position_size_pct(direction: int) -> float:
        """How much of free cash to allocate."""
        return 1.0 if direction == 1 else 0.5

    for _, r in df.iterrows():
        # Close matured positions first
        still_open = []
        for p in open_positions:
            if p["exit_time"] <= r["signal_time"]:
                # Realize P/L
                equity *= (1 + p["pnl_pct"] / 100)
                free_cash += p["allocated"]  # return cash
                events.append({
                    "exit_time": p["exit_time"],
                    "ticker": p.get("ticker", ""),
                    "direction": p["direction"],
                    "entry": p["entry"],
                    "sl": p["sl"],
                    "tp1": p["tp1"],
                    "outcome": p["outcome"],
                    "pnl_pct": p["pnl_pct"],
                    "equity_after": equity,
                })
            else:
                still_open.append(p)
        open_positions = still_open

        # Determine size for new position
        direction = 1 if r.get("direction", "BUY") in ("BUY", 1, "1") else -1
        size_pct = position_size_pct(direction)
        allocated = free_cash * size_pct

        # Skip if not enough cash (e.g. need 100% but only 50% free)
        if allocated < 0.01 or (direction == 1 and size_pct > free_cash + 1e-9):
            skipped_no_cash += 1
            continue

        entry = float(r["entry"])
        sl = float(r["sl"])
        tp1 = float(r.get("tp1") or 0.0)
        outcome = r.get("outcome", "")

        # Compute P/L based on outcome
        if direction == 1:  # LONG
            if outcome == "WIN_TP1":
                pnl_pct = ((tp1 - entry) / entry) * 100
            elif outcome == "LOSS":
                pnl_pct = ((sl - entry) / entry) * 100  # negative
            else:  # EXPIRED, NO_TP
                # Use actual_rr if available, else 0
                actual_rr = float(r.get("actual_rr", 0) or 0)
                risk_pct = abs((sl - entry) / entry) * 100
                pnl_pct = actual_rr * risk_pct if actual_rr > 0 else 0
        else:  # SHORT
            if outcome == "WIN_TP1":
                pnl_pct = ((entry - tp1) / entry) * 100 * 0.5
            elif outcome == "LOSS":
                pnl_pct = ((entry - sl) / entry) * 100 * 0.5  # sl>entry → negative
            else:
                actual_rr = float(r.get("actual_rr", 0) or 0)
                risk_pct = abs((sl - entry) / entry) * 100
                pnl_pct = actual_rr * risk_pct * 0.5 if actual_rr > 0 else 0

        # Scale by allocated fraction of equity
        # pnl_pct above is for "full position"; scale by (allocated / equity_at_entry)
        # Since we track equity and free_cash separately, scale by allocated
        pnl_pct_scaled = pnl_pct * allocated

        open_positions.append({
            "exit_time": r["exit_time"],
            "ticker": r.get("ticker", ""),
            "direction": "LONG" if direction == 1 else "SHORT",
            "entry": entry,
            "sl": sl,
            "tp1": tp1,
            "outcome": outcome,
            "pnl_pct": pnl_pct_scaled,
            "allocated": allocated,
        })
        free_cash -= allocated
        taken += 1

    # Close remaining
    for p in open_positions:
        equity *= (1 + p["pnl_pct"] / 100)
        events.append({
            "exit_time": p["exit_time"],
            "ticker": p.get("ticker", ""),
            "direction": p["direction"],
            "entry": p["entry"],
            "sl": p["sl"],
            "tp1": p["tp1"],
            "outcome": p["outcome"],
            "pnl_pct": p["pnl_pct"],
            "equity_after": equity,
        })

    stats = {
        "taken": taken,
        "skipped_no_cash": skipped_no_cash,
        "final_equity": equity,
    }
    return pd.DataFrame(events), stats


def monthly_table(events: pd.DataFrame, signals: pd.DataFrame) -> pd.DataFrame:
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
            "return_pct": round(ret_pct, 2),
            "closes": int(len(eg)),
        })
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("signals_csv")
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    sig = pd.read_csv(args.signals_csv)
    if "ticker" not in sig.columns:
        sig["ticker"] = sig.get("pair", "?")

    sig["time"] = pd.to_datetime(sig["signal_time"], utc=True, format="ISO8601")
    period_start = sig["time"].min().strftime("%Y-%m-%d")
    period_end = sig["time"].max().strftime("%Y-%m-%d")

    print("=" * 90)
    if args.label:
        print(f"  {args.label}")
        print("  " + "-" * 86)
    print(f"  Period: {period_start} → {period_end}")
    print(f"  Risk model: FULL DEPOSIT (LONG=100%, SHORT=50%, no leverage, single-position)")
    print("=" * 90)

    events, stats = simulate_full_deposit(sig)
    monthly = monthly_table(events, sig)

    print(f"\n{'Month':<8} {'Signals':>8} {'W':>4} {'L':>4} {'WR%':>6} {'Return%':>10} {'Closes':>7}")
    print("-" * 56)
    for _, r in monthly.iterrows():
        print(f"{r['month']:<8} {r['signals']:>8} {r['wins']:>4} {r['losses']:>4} "
              f"{r['win_rate']:>5.1f}% {r['return_pct']:>+9.2f}% {r['closes']:>7}")
    print("-" * 56)

    total_signals = monthly["signals"].sum()
    total_wins = monthly["wins"].sum()
    total_losses = monthly["losses"].sum()
    total_wr = total_wins / max(total_wins + total_losses, 1) * 100
    final_ret = (stats["final_equity"] - 1) * 100
    n_pos = (monthly["return_pct"] > 0).sum()
    n_neg = (monthly["return_pct"] < 0).sum()

    print(f"{'TOTAL':<8} {total_signals:>8} {total_wins:>4} {total_losses:>4} "
          f"{total_wr:>5.1f}% {final_ret:>+9.2f}%")
    print()
    print(f"  Months: {len(monthly)}  (positive: {n_pos}, negative: {n_neg})")
    print(f"  Avg month: {monthly['return_pct'].mean():+.2f}%, median: {monthly['return_pct'].median():+.2f}%")
    print(f"  Best month: {monthly['return_pct'].max():+.2f}% ({monthly.loc[monthly['return_pct'].idxmax(), 'month']})")
    print(f"  Worst month: {monthly['return_pct'].min():+.2f}% ({monthly.loc[monthly['return_pct'].idxmin(), 'month']})")
    print(f"  Trades taken: {stats['taken']}, skipped (no cash): {stats['skipped_no_cash']}")
    print(f"  Final equity: {stats['final_equity']:.4f}x  (return: {final_ret:+.2f}%)")

    return 0


if __name__ == "__main__":
    main()
