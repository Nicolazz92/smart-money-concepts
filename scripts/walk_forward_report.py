#!/usr/bin/env python3
"""Compare Walk-Forward OOS results vs Baseline (fixed author-default params).

Reads:
  data/wf_{venue}_oos{N}m_signals.csv   — WF concatenated OOS signals
  data/wf_{venue}_oos{N}m_params.csv    — per-step chosen parameters

Computes:
  1. WF portfolio simulation (with concurrency cap, same as analyze_returns.py)
  2. Baseline portfolio simulation on the SAME total OOS period
     (using author-default params: bias_swing=10, ob_swing=10,
      sl_buffer=50 MOEX / 200 Bybit, min_rr=1.0)
  3. Edge retention % = WF_total_R / Baseline_total_R

Usage:
    python scripts/walk_forward_report.py --venue bybit --oos-months 1
    python scripts/walk_forward_report.py --venue moex --oos-months 3
"""
from __future__ import annotations

import argparse
import json
import os
import sys
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

# Author defaults (baseline) — the params the universe backtest used
BASELINE_PARAMS = {
    "bias_swing": 10,
    "ob_swing": 10,
    "min_rr": 1.0,
}
VENUE_DEFAULTS = {
    "bybit": {"sl_buffer": 200.0, "suffix": "_bybit", "cost": "bybit-perp",
              "killzones": [], "data_start": "2023-10-25"},
    "moex":  {"sl_buffer": 50.0,  "suffix": "",        "cost": "strateg",
              "killzones": ["MOEX main session"], "data_start": "2023-03-01"},
}


def compute_period_returns(events_df: pd.DataFrame, freq: str) -> pd.DataFrame:
    if events_df.empty:
        return pd.DataFrame()
    df = events_df.copy()
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time").sort_index()
    df["period"] = df.index.tz_convert(None).to_period(freq)
    out = []
    for period, group in df.groupby("period"):
        period_ret = 1.0
        for pnl in group["pnl_pct"]:
            period_ret *= (1 + pnl / 100)
        out.append({
            "period": str(period),
            "n_closes": len(group),
            "return_pct": (period_ret - 1) * 100,
        })
    return pd.DataFrame(out)


def simulate_portfolio(signals: pd.DataFrame, risk_pct: float,
                       max_concurrent: int) -> tuple[pd.DataFrame, dict]:
    """Same logic as analyze_returns.simulate_portfolio."""
    df = signals.dropna(subset=["actual_rr"]).copy()
    df["signal_time"] = pd.to_datetime(df["signal_time"], utc=True,
                                        format="ISO8601")
    if "exit_time" in df.columns:
        df["exit_time"] = pd.to_datetime(df["exit_time"], utc=True,
                                          format="ISO8601", errors="coerce")
    else:
        df["exit_time"] = pd.NaT
    if df["exit_time"].isna().all():
        df["exit_time"] = df["signal_time"] + pd.Timedelta(days=5)
    df = df.sort_values("signal_time").reset_index(drop=True)

    equity = 1.0
    open_pos = []
    events = []
    skipped = 0
    taken = 0

    for _, r in df.iterrows():
        # Close matured
        still = []
        for p in open_pos:
            if p["exit_time"] <= r["signal_time"]:
                pnl_pct = risk_pct * p["rr"]
                equity *= (1 + pnl_pct)
                events.append({"time": p["exit_time"], "rr": p["rr"],
                              "pnl_pct": pnl_pct * 100, "equity": equity,
                              "action": "CLOSE"})
            else:
                still.append(p)
        open_pos = still

        if len(open_pos) < max_concurrent:
            open_pos.append({"exit_time": r["exit_time"], "rr": float(r["actual_rr"])})
            taken += 1
        else:
            skipped += 1

    for p in open_pos:
        pnl_pct = risk_pct * p["rr"]
        equity *= (1 + pnl_pct)
        events.append({"time": p["exit_time"], "rr": p["rr"],
                      "pnl_pct": pnl_pct * 100, "equity": equity,
                      "action": "CLOSE"})

    stats = {"taken": taken, "skipped": skipped, "final_equity": equity}
    return pd.DataFrame(events), stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", choices=["bybit", "moex"], required=True)
    ap.add_argument("--oos-months", type=int, required=True)
    ap.add_argument("--risk", type=float, default=0.01)
    ap.add_argument("--max-concurrent", type=int, default=5)
    ap.add_argument("--ob-tf", default="60M",
                    help="OB timeframe (60M or 4H). Affects filename suffix.")
    args = ap.parse_args()

    tf_suffix = "" if args.ob_tf == "60M" else f"_{args.ob_tf}"
    tag = f"{args.venue}_oos{args.oos_months}m{tf_suffix}"
    wf_sig_path = f"data/wf_{tag}_signals.csv"
    wf_params_path = f"data/wf_{tag}_params.csv"
    wf_summary_path = f"data/wf_{tag}_summary.json"

    if not Path(wf_sig_path).exists():
        print(f"ERROR: WF signals not found: {wf_sig_path}")
        print("Run walk_forward.py first.")
        return 1

    wf_signals = pd.read_csv(wf_sig_path)
    wf_params = pd.read_csv(wf_params_path)
    wf_summary = json.load(open(wf_summary_path, encoding="utf-8"))

    # Determine total OOS period from the WF run
    oos_start = wf_signals["oos_start"].min() if len(wf_signals) > 0 else None
    oos_end = wf_signals["oos_end"].max() if len(wf_signals) > 0 else None

    print("=" * 78)
    print(f"WALK-FORWARD vs BASELINE  ({args.venue}, OOS={args.oos_months}m)")
    print("=" * 78)
    print(f"WF total OOS period: {oos_start} → {oos_end}")
    print(f"WF signals: {len(wf_signals)}")
    print()

    # ── Simulate WF portfolio
    wf_events, wf_stats = simulate_portfolio(wf_signals, args.risk, args.max_concurrent)
    wf_total_return = (wf_stats["final_equity"] - 1) * 100

    # ── Run BASELINE backtest on same period (need tickers from WF config)
    venue_cfg = VENUE_DEFAULTS[args.venue]
    tickers = wf_summary["tickers"]
    provider = CsvProvider(data_dir="data", filename_suffix=venue_cfg["suffix"])
    provider.connect()
    strategy = StrategyConfig(
        pairs=tickers,
        bias_timeframe="1D", ob_timeframe=args.ob_tf,
        bias_swing_length=BASELINE_PARAMS["bias_swing"],
        ob_swing_length=BASELINE_PARAMS["ob_swing"],
        sl_buffer_pips=venue_cfg["sl_buffer"],
        killzones=venue_cfg["killzones"],
    )
    engine = BacktestEngine(
        provider=provider, strategy=strategy, notifier=None,
        min_rr=BASELINE_PARAMS["min_rr"],
        cost_model=FINAM_PRESETS[venue_cfg["cost"]],
    )
    res = engine.run(start_date=oos_start, end_date=oos_end)
    baseline_signals_rows = []
    for s in res.signals:
        baseline_signals_rows.append({
            "signal_time": s.signal_time,
            "exit_time": s.exit_time if s.exit_time else (
                s.signal_time + pd.Timedelta(days=5)),
            "actual_rr": s.actual_rr if s.actual_rr is not None else 0.0,
            "outcome": s.outcome,
            "ticker": s.pair,
        })
    baseline_signals = pd.DataFrame(baseline_signals_rows)
    base_events, base_stats = simulate_portfolio(
        baseline_signals, args.risk, args.max_concurrent)
    base_total_return = (base_stats["final_equity"] - 1) * 100

    # ── Edge retention
    wf_total_rr = wf_signals["actual_rr"].sum()
    base_total_rr = baseline_signals["actual_rr"].sum()
    edge_retention = (wf_total_rr / base_total_rr * 100) if base_total_rr > 0 else 0

    print("─" * 78)
    print("CORE COMPARISON")
    print("─" * 78)
    print(f"  {'Metric':<30}  {'WF':>15}  {'Baseline':>15}")
    print(f"  {'Signals':<30}  {len(wf_signals):>15}  {len(baseline_signals):>15}")
    wf_decided = wf_signals["actual_rr"].notna().sum()
    base_decided = baseline_signals["actual_rr"].notna().sum()
    print(f"  {'Decided trades':<30}  {wf_decided:>15}  {base_decided:>15}")
    wf_wr = ((wf_signals["outcome"] == "WIN_TP1").sum() / max(wf_decided, 1)) * 100
    base_wr = ((baseline_signals["outcome"] == "WIN_TP1").sum() / max(base_decided, 1)) * 100
    print(f"  {'Win rate %':<30}  {wf_wr:>15.1f}  {base_wr:>15.1f}")
    print(f"  {'Total R':<30}  {wf_total_rr:>+15.2f}  {base_total_rr:>+15.2f}")
    wf_exp = wf_total_rr / max(wf_decided, 1)
    base_exp = base_total_rr / max(base_decided, 1)
    print(f"  {'Expectancy (R/trade)':<30}  {wf_exp:>+15.4f}  {base_exp:>+15.4f}")
    print()
    print(f"  Total return (1% risk, 5 concurrent):")
    print(f"    WF:        {wf_total_return:>+10.2f}%   (equity x{wf_stats['final_equity']:.3f})")
    print(f"    Baseline:  {base_total_return:>+10.2f}%   (equity x{base_stats['final_equity']:.3f})")
    print()
    print(f"  >>> EDGE RETENTION: {edge_retention:.1f}%  "
          f"(WF retained {edge_retention:.1f}% of baseline's total R)")
    print()

    # ── Period returns comparison
    print("─" * 78)
    print("PERIOD RETURN COMPARISON")
    print("─" * 78)
    for freq, name in [("Y", "YEARLY"), ("Q", "QUARTERLY")]:
        wf_pr = compute_period_returns(wf_events, freq)
        base_pr = compute_period_returns(base_events, freq)
        if wf_pr.empty and base_pr.empty:
            continue
        merged = pd.merge(wf_pr, base_pr, on="period",
                         how="outer", suffixes=("_WF", "_BASE")).fillna(0)
        merged = merged.sort_values("period")
        print(f"\n{name}:")
        cols = ["period", "return_pct_WF", "return_pct_BASE"]
        print(merged[cols].to_string(index=False))

    # ── Parameter analysis
    print()
    print("─" * 78)
    print("PARAMETER STABILITY (overfit check)")
    print("─" * 78)
    for col in ["best_bias_swing", "best_ob_swing",
                "best_sl_buffer", "best_min_rr"]:
        counts = wf_params[col].value_counts()
        most = counts.index[0]
        share = counts.iloc[0] / len(wf_params) * 100
        print(f"  {col:25}: {dict(counts)}  "
              f"→ most often {most} ({share:.0f}% of steps)")

    # IS vs OOS expectancy gap (overfit indicator)
    avg_is = wf_params["is_expectancy"].mean()
    avg_oos = wf_params["oos_expectancy"].mean()
    print()
    print(f"  Avg IS expectancy (best):    {avg_is:+.4f}")
    print(f"  Avg OOS expectancy:          {avg_oos:+.4f}")
    if avg_is > 0:
        print(f"  OOS/IS ratio:                {avg_oos/avg_is*100:.1f}%")
        print(f"  (100% = perfect, <30% = severe overfit, >70% = robust)")

    # ── Save final comparison
    out = {
        "venue": args.venue,
        "oos_months": args.oos_months,
        "wf_total_return_pct": round(wf_total_return, 2),
        "baseline_total_return_pct": round(base_total_return, 2),
        "wf_total_rr": round(wf_total_rr, 2),
        "baseline_total_rr": round(base_total_rr, 2),
        "edge_retention_pct": round(edge_retention, 1),
        "wf_win_rate": round(wf_wr, 1),
        "baseline_win_rate": round(base_wr, 1),
        "wf_expectancy": round(wf_exp, 4),
        "baseline_expectancy": round(base_exp, 4),
        "avg_is_expectancy": round(avg_is, 4),
        "avg_oos_expectancy": round(avg_oos, 4),
        "oos_is_ratio_pct": round(avg_oos / avg_is * 100, 1) if avg_is > 0 else None,
    }
    out_path = f"data/wf_{tag}_report.json"
    json.dump(out, open(out_path, "w", encoding="utf-8"), indent=2)
    print(f"\nReport saved: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
