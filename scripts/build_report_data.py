#!/usr/bin/env python3
"""Build per-month statistics for the 6 top runs (3 Bybit + 3 MOEX).

For each run, produce:
  - Per-month: signals, decided, WR, total R, expectancy, avg signals/ticker
  - Overall: total signals, total R, avg WR, avg signals/ticker/month
  - Tickers traded, with their share of signals

Output: data/runs_stats.json (used by REPORT_PROJECT.md)
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def per_month_stats(df: pd.DataFrame) -> list[dict]:
    """Group signals by month. Returns list of monthly dicts."""
    d = df.dropna(subset=["actual_rr"]).copy()
    d["time"] = pd.to_datetime(d["signal_time"], format="ISO8601", utc=True)
    d["ym"] = d["time"].dt.strftime("%Y-%m")
    rows = []
    for ym, g in d.groupby("ym"):
        wins = (g["outcome"] == "WIN_TP1").sum()
        losses = (g["outcome"] == "LOSS").sum()
        decided = wins + losses
        wr = wins / decided * 100 if decided > 0 else 0
        total_rr = g["actual_rr"].sum()
        exp = total_rr / decided if decided > 0 else 0
        n_tickers = g["ticker"].nunique() if "ticker" in g.columns else 1
        rows.append({
            "month": ym,
            "signals": int(len(g)),
            "decided": int(decided),
            "wins": int(wins),
            "losses": int(losses),
            "win_rate": round(wr, 1),
            "total_rr": round(total_rr, 2),
            "expectancy": round(exp, 4),
            "tickers_traded": int(n_tickers),
            "signals_per_ticker": round(len(g) / max(n_tickers, 1), 2),
        })
    return rows


def overall_stats(df: pd.DataFrame, monthly: list) -> dict:
    d = df.dropna(subset=["actual_rr"])
    wins = (d["outcome"] == "WIN_TP1").sum()
    losses = (d["outcome"] == "LOSS").sum()
    decided = wins + losses
    months = [m["month"] for m in monthly]
    return {
        "period": f"{min(months)} → {max(months)}" if months else "",
        "n_months": len(months),
        "total_signals": int(len(d)),
        "decided": int(decided),
        "wins": int(wins),
        "losses": int(losses),
        "win_rate": round(wins / decided * 100, 2) if decided > 0 else 0,
        "total_rr": round(d["actual_rr"].sum(), 2),
        "expectancy": round(d["actual_rr"].sum() / decided, 4) if decided > 0 else 0,
        "avg_signals_per_month": round(len(d) / max(len(months), 1), 1),
        "n_tickers_traded": int(d["ticker"].nunique()) if "ticker" in d.columns else 1,
        "best_month": max(monthly, key=lambda m: m["total_rr"])["month"] if monthly else "",
        "worst_month": min(monthly, key=lambda m: m["total_rr"])["month"] if monthly else "",
    }


def per_ticker(df: pd.DataFrame, top_n: int = 10) -> list[dict]:
    d = df.dropna(subset=["actual_rr"])
    rows = []
    for tkr, g in d.groupby("ticker"):
        wins = (g["outcome"] == "WIN_TP1").sum()
        losses = (g["outcome"] == "LOSS").sum()
        decided = wins + losses
        wr = wins / decided * 100 if decided > 0 else 0
        rows.append({
            "ticker": tkr,
            "signals": int(len(g)),
            "win_rate": round(wr, 1),
            "total_rr": round(g["actual_rr"].sum(), 2),
            "share_pct": round(len(g) / len(d) * 100, 1),
        })
    rows.sort(key=lambda x: -x["total_rr"])
    return rows[:top_n]


def main():
    runs = [
        # Bybit 1D+60M
        {
            "id": "bybit_wf_1m",
            "venue": "Bybit",
            "label": "Bybit Walk-Forward OOS=1 мес (1D+60M)",
            "signals_csv": "data/wf_bybit_oos1m_signals.csv",
            "type": "WF (out-of-sample)",
            "tf": "1D + 60M",
            "tickers": 7,
            "best_params": "swing=10/10, sl_buffer=200 pips, min_rr=1.5",
            "cost": "bybit-perp (0.135% round-trip)",
            "period_desc": "Dec 2023 → Jun 2026",
        },
        {
            "id": "bybit_wf_3m",
            "venue": "Bybit",
            "label": "Bybit Walk-Forward OOS=3 мес (1D+60M)",
            "signals_csv": "data/wf_bybit_oos3m_signals.csv",
            "type": "WF (out-of-sample)",
            "tf": "1D + 60M",
            "tickers": 7,
            "best_params": "swing=10/10, sl_buffer=200 pips, min_rr=1.5",
            "cost": "bybit-perp (0.135% round-trip)",
            "period_desc": "Nov 2023 → Jun 2026",
        },
        # Bybit 1D+4H — new!
        {
            "id": "bybit_wf_1m_4H",
            "venue": "Bybit",
            "label": "Bybit Walk-Forward OOS=1 мес (1D+4H) 🏆",
            "signals_csv": "data/wf_bybit_oos1m_4H_signals.csv",
            "type": "WF (out-of-sample)",
            "tf": "1D + 4H",
            "tickers": 7,
            "best_params": "swing=10/10, sl_buffer=200 pips, min_rr=1.5",
            "cost": "bybit-perp (0.135% round-trip)",
            "period_desc": "Dec 2023 → Jun 2026",
        },
        {
            "id": "bybit_wf_3m_4H",
            "venue": "Bybit",
            "label": "Bybit Walk-Forward OOS=3 мес (1D+4H) 🏆",
            "signals_csv": "data/wf_bybit_oos3m_4H_signals.csv",
            "type": "WF (out-of-sample)",
            "tf": "1D + 4H",
            "tickers": 7,
            "best_params": "swing=10/10, sl_buffer=200 pips, min_rr=1.5",
            "cost": "bybit-perp (0.135% round-trip)",
            "period_desc": "Nov 2023 → Jun 2026",
        },
        # Bybit universe 1D+4H (in-sample)
        {
            "id": "bybit_1D4H",
            "venue": "Bybit",
            "label": "Bybit Universe бэктест (1D+4H)",
            "signals_csv": "data/bybit_1D4H_signals.csv",
            "type": "Universe (in-sample)",
            "tf": "1D + 4H",
            "tickers": 8,
            "best_params": "swing=10/10, sl_buffer=200 pips, min_rr=1.5",
            "cost": "bybit-perp (0.135% round-trip)",
            "period_desc": "Oct 2023 → Jul 2026",
        },
        # MOEX
        {
            "id": "moex_wf_1m",
            "venue": "MOEX",
            "label": "MOEX Walk-Forward OOS=1 мес (1D+60M)",
            "signals_csv": "data/wf_moex_oos1m_signals.csv",
            "type": "WF (out-of-sample)",
            "tf": "1D + 60M",
            "tickers": 20,
            "best_params": "swing=10/10, sl_buffer=25 pips, min_rr=1.5",
            "cost": "Финам Стратег (0.26% round-trip)",
            "period_desc": "Apr 2023 → Jul 2026",
        },
        {
            "id": "moex_wf_3m",
            "venue": "MOEX",
            "label": "MOEX Walk-Forward OOS=3 мес (1D+60M)",
            "signals_csv": "data/wf_moex_oos3m_signals.csv",
            "type": "WF (out-of-sample)",
            "tf": "1D + 60M",
            "tickers": 20,
            "best_params": "swing=10/10, sl_buffer=25 pips, min_rr=1.5",
            "cost": "Финам Стратег (0.26% round-trip)",
            "period_desc": "Apr 2023 → Jul 2026",
        },
        {
            "id": "moex_1D4H",
            "venue": "MOEX",
            "label": "MOEX Universe бэктест (1D+4H)",
            "signals_csv": "data/moex_1D4H_signals.csv",
            "type": "Universe (in-sample)",
            "tf": "1D + 4H",
            "tickers": 45,
            "best_params": "swing=10/10, sl_buffer=25 pips, min_rr=1.5",
            "cost": "Финам Стратег (0.26% round-trip)",
            "period_desc": "Mar 2023 → Jul 2026",
        },
    ]

    out = {}
    for r in runs:
        path = r["signals_csv"]
        if not Path(path).exists():
            print(f"SKIP {r['id']}: {path} missing")
            continue
        df = pd.read_csv(path)
        # Ensure ticker column exists (universe CSVs have it; WF have it too)
        if "ticker" not in df.columns:
            df["ticker"] = df.get("pair", "UNKNOWN")
        monthly = per_month_stats(df)
        overall = overall_stats(df, monthly)
        top_tickers = per_ticker(df)
        out[r["id"]] = {
            **r,
            "overall": overall,
            "monthly": monthly,
            "top_tickers": top_tickers,
        }
        print(f"{r['id']}: {overall['total_signals']} signals, "
              f"WR {overall['win_rate']}%, R {overall['total_rr']}, "
              f"{overall['n_months']} months, "
              f"avg {overall['avg_signals_per_month']} sig/month")

    json.dump(out, open("data/runs_stats.json", "w", encoding="utf-8"),
              indent=2, ensure_ascii=False, default=str)
    print(f"\nSaved: data/runs_stats.json ({len(out)} runs)")


if __name__ == "__main__":
    main()
