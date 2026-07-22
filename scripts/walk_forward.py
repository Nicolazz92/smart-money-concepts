#!/usr/bin/env python3
"""Anchored expanding walk-forward optimization for the SMC strategy.

Method
------
For each step t = 0, 1, 2, ...:
  IS window  = [data_start, data_start + (t+1)*step_months]
                (anchored — grows from start; never rolls)
  OOS window = [IS_end, IS_end + oos_months]

  1. On IS, evaluate ALL grid combinations in parallel (multiprocessing).
     Pick the one with highest EXPECTANCY (avg R per trade).
     Require >= min_is_trades, otherwise penalise (prefer larger samples).
  2. Evaluate the chosen params on the OOS window — this is "trading" with
     parameters selected without seeing this period.
  3. Concatenate OOS signals across all steps → final WF equity curve.

The result is comparable to a baseline (fixed author-default params) run on
the same total OOS period. Edge retention = WF_total_R / baseline_total_R.

Usage
-----
    python scripts/walk_forward.py --venue bybit --oos-months 1
    python scripts/walk_forward.py --venue moex  --oos-months 3 --step-months 1
    python scripts/walk_forward.py --smoke   # tiny grid, 3 tickers, 2 steps
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from itertools import product
from pathlib import Path

import pandas as pd
from joblib import Parallel, delayed

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

# ─── Universe presets ───────────────────────────────────────────────────────
BYBIT_TICKERS = ["BTCUSDT", "ETHUSDT", "BTCPERP", "SOLUSDT",
                 "ZECUSDT", "LTCUSDT", "HYPEUSDT"]
MOEX_TOP20 = ["ENPG", "NVTK", "OZON", "FLOT", "VKCO", "TRNFP", "SNGSP",
              "CHMF", "PHOR", "AFKS", "NLMK", "FESH", "POSI", "MAGN",
              "ETLN", "RUAL", "SNGS", "IRKT", "MGNT", "MOEX"]


# ─── Param grid ─────────────────────────────────────────────────────────────
def build_grid(venue: str, smoke: bool = False, extended: bool = False) -> list[dict]:
    """Return list of all parameter combinations to evaluate on IS.

    Three grid sizes:
      - smoke: 6 combos (2 buffers × 3 min_rrs)
      - normal: 9 combos (3 buffers × 3 min_rrs), swing fixed at 10/10
      - extended: 27 combos (3 swings × 3 buffers × 3 min_rrs), swing varied
    """
    if extended:
        bias_swings = [5, 10, 15]
        ob_swings = [10]  # keep ob_swing fixed, vary only bias_swing
        sl_buffers = [100, 200, 400] if venue == "bybit" else [25, 50, 100]
        min_rrs = [0.5, 1.0, 1.5]
    elif smoke:
        bias_swings = [10]
        ob_swings = [10]
        sl_buffers = [100, 200] if venue == "bybit" else [25, 50]
        min_rrs = [0.5, 1.0, 1.5]
    else:
        bias_swings = [10]
        ob_swings = [10]
        sl_buffers = [100, 200, 400] if venue == "bybit" else [25, 50, 100]
        min_rrs = [0.5, 1.0, 1.5]
    return [
        {"bias_swing": bs, "ob_swing": os_, "sl_buffer": sb, "min_rr": mr}
        for bs, os_, sb, mr in product(bias_swings, ob_swings, sl_buffers, min_rrs)
    ]


# ─── Provider cache (per-process, lazy) ────────────────────────────────────
# CsvProvider loads ALL CSVs from disk on first access. Creating it per-call
# is the main bottleneck — we re-read 80+ files every time. Cache it.
_PROVIDER_CACHE: dict[tuple[str, str], CsvProvider] = {}


def _get_provider(data_dir: str, filename_suffix: str) -> CsvProvider:
    """Return cached CsvProvider, creating if needed."""
    key = (data_dir, filename_suffix)
    if key not in _PROVIDER_CACHE:
        p = CsvProvider(data_dir=data_dir, filename_suffix=filename_suffix)
        p.connect()
        _PROVIDER_CACHE[key] = p
    return _PROVIDER_CACHE[key]


# ─── Single backtest (module-level, uses cached provider) ──────────────────
def eval_one(params, start, end, tickers, label,
             data_dir="data", filename_suffix="",
             cost_preset="strateg", killzones=None,
             bias_tf="1D", ob_tf="60M"):
    """Run one backtest with given params on a window. Returns metrics dict.

    Uses module-level provider cache to avoid re-loading CSVs every call.
    Safe for both single-process sequential use and joblib workers
    (each worker process has its own _PROVIDER_CACHE).
    """
    provider = _get_provider(data_dir, filename_suffix)
    strategy = StrategyConfig(
        pairs=tickers,
        bias_timeframe=bias_tf,
        ob_timeframe=ob_tf,
        bias_swing_length=params["bias_swing"],
        ob_swing_length=params["ob_swing"],
        sl_buffer_pips=params["sl_buffer"],
        killzones=killzones or [],
    )
    engine = BacktestEngine(
        provider=provider, strategy=strategy, notifier=None,
        min_rr=params["min_rr"], cost_model=FINAM_PRESETS[cost_preset],
    )
    res = engine.run(start_date=start, end_date=end)
    decided = res.wins + res.losses
    expectancy = (res.total_rr / decided) if decided > 0 else 0.0
    return {
        "label": label,
        "bias_swing": params["bias_swing"],
        "ob_swing": params["ob_swing"],
        "sl_buffer": params["sl_buffer"],
        "min_rr": params["min_rr"],
        "signals": res.total_signals,
        "wins": res.wins,
        "losses": res.losses,
        "win_rate": res.win_rate,
        "total_rr": round(res.total_rr, 2),
        "expectancy": round(expectancy, 4),
        "decided": decided,
        "_signals_list": res.signals,  # only used for OOS export
    }


# ─── Date helpers ───────────────────────────────────────────────────────────
def add_months(d: datetime, months: int) -> datetime:
    """Add months to a datetime, clamping day to month end if needed."""
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    # Clamp day (e.g. Jan 31 + 1 month → Feb 28)
    import calendar
    last_day = calendar.monthrange(y, m)[1]
    return d.replace(year=y, month=m, day=min(d.day, last_day))


# ─── Main WF driver ─────────────────────────────────────────────────────────
@dataclass
class WFConfig:
    venue: str
    tickers: list[str]
    data_start: str
    data_end: str
    step_months: int
    oos_months: int
    min_is_trades: int
    out_signals: str
    out_params: str
    out_summary: str
    ob_tf: str = "60M"


def run_walk_forward(cfg: WFConfig, smoke: bool = False, extended: bool = False) -> int:
    grid = build_grid(cfg.venue, smoke=smoke, extended=extended)
    n_grid = len(grid)
    print(f"=== WALK-FORWARD ({cfg.venue}, {len(cfg.tickers)} tickers, "
          f"grid={n_grid}, OOS={cfg.oos_months}m, step={cfg.step_months}m) ===")
    print(f"Period: {cfg.data_start} → {cfg.data_end}")

    # Venue-specific worker kwargs
    if cfg.venue == "bybit":
        suffix = "_bybit"
        cost = "bybit-perp"
        killzones = []
        bias_tf, ob_tf = "1D", cfg.ob_tf
    else:
        suffix = ""
        cost = "strateg"
        killzones = ["MOEX main session"]
        bias_tf, ob_tf = "1D", cfg.ob_tf
    worker_kwargs = {
        "venue": cfg.venue, "cost_preset": cost,
        "killzones": killzones, "bias_tf": bias_tf, "ob_tf": ob_tf,
    }

    # Generate step windows
    start_dt = datetime.strptime(cfg.data_start, "%Y-%m-%d")
    end_dt = datetime.strptime(cfg.data_end, "%Y-%m-%d")
    steps = []
    t = 0
    while True:
        is_end = add_months(start_dt, (t + 1) * cfg.step_months)
        oos_start = is_end
        oos_end = add_months(oos_start, cfg.oos_months)
        if oos_end > end_dt:
            break
        steps.append((t, is_end, oos_start, oos_end))
        t += 1
    print(f"Steps: {len(steps)}  (first IS ends {steps[0][1].date()}, "
          f"last OOS ends {steps[-1][3].date()})")
    print()

    n_cpu = max(1, os.cpu_count() or 1)
    # On Windows, loky (joblib default) handles spawn reliably.
    n_workers = max(1, n_cpu - 1)
    # Allow override via env: WF_SEQUENTIAL=1 disables parallel
    force_seq = os.environ.get("WF_SEQUENTIAL", "") == "1"
    use_parallel = n_workers >= 2 and not smoke and not force_seq
    if use_parallel:
        print(f"Using {n_workers} joblib workers (loky backend) for parallel grid.")
    else:
        print("Running sequentially (single worker).")
    print()

    all_oos_signals = []
    per_step_rows = []
    t0 = time.time()

    # Common kwargs for every eval_one call
    eval_kwargs = dict(
        data_dir="data",
        filename_suffix=suffix,
        cost_preset=cost,
        killzones=killzones,
        bias_tf=bias_tf,
        ob_tf=ob_tf,
    )

    # ─── Resume support: if checkpoint exists, load progress ─────────────
    tf_suffix = "" if cfg.ob_tf == "60M" else f"_{cfg.ob_tf}"
    checkpoint_path = f"data/.wf_{cfg.venue}_oos{cfg.oos_months}m{tf_suffix}_ckpt.json"
    start_from_step = 0
    if Path(checkpoint_path).exists() and not smoke:
        try:
            ckpt = json.load(open(checkpoint_path, encoding="utf-8"))
            if (ckpt.get("tickers") == cfg.tickers
                    and ckpt.get("data_start") == cfg.data_start
                    and len(ckpt.get("per_step_rows", [])) > 0):
                per_step_rows = ckpt["per_step_rows"]
                all_oos_signals = ckpt["all_oos_signals"]
                start_from_step = len(per_step_rows)
                print(f"RESUMED from checkpoint: {start_from_step} steps already done")
                print(f"  Checkpoint: {checkpoint_path}")
        except Exception as e:
            print(f"Checkpoint load failed ({e}), starting fresh.")

    for step_idx, is_end, oos_start, oos_end in steps:
        if step_idx < start_from_step:
            continue  # already done in checkpoint
        ts = time.time()
        is_end_str = is_end.strftime("%Y-%m-%d")
        oos_start_str = oos_start.strftime("%Y-%m-%d")
        oos_end_str = oos_end.strftime("%Y-%m-%d")

        # ── Evaluate ALL grid combos on IS (parallel via joblib, or sequential)
        if use_parallel:
            is_results = Parallel(n_jobs=n_workers, backend="loky", verbose=0)(
                delayed(eval_one)(p, cfg.data_start, is_end_str, cfg.tickers, "IS", **eval_kwargs)
                for p in grid
            )
        else:
            is_results = [
                eval_one(p, cfg.data_start, is_end_str, cfg.tickers, "IS", **eval_kwargs)
                for p in grid
            ]

        # ── Pick best by expectancy (with min-trades guard)
        valid = [r for r in is_results if r["decided"] >= cfg.min_is_trades]
        if not valid:
            valid = is_results
            print(f"  [step {step_idx}] WARNING: no combo reached "
                  f"{cfg.min_is_trades} IS trades, picking max-trades")
        best = max(valid, key=lambda r: r["expectancy"])
        avg_expect = sum(r["expectancy"] for r in is_results) / len(is_results)

        # ── Evaluate BEST on OOS (single call, no pool needed)
        best_params = {
            "bias_swing": best["bias_swing"],
            "ob_swing": best["ob_swing"],
            "sl_buffer": best["sl_buffer"],
            "min_rr": best["min_rr"],
        }
        oos_result = eval_one(best_params, oos_start_str, oos_end_str,
                              cfg.tickers, "OOS", **eval_kwargs)

        # ── Collect signals for equity curve
        for s in oos_result["_signals_list"]:
            all_oos_signals.append({
                "step": step_idx,
                "oos_start": oos_start_str,
                "oos_end": oos_end_str,
                "ticker": s.pair,
                "signal_time": s.signal_time,
                "direction": "BUY" if s.direction == 1 else "SELL",
                "entry": s.entry, "sl": s.sl, "tp1": s.tp1,
                "rr1": s.rr1, "outcome": s.outcome,
                "actual_rr": s.actual_rr,
                "params_used": json.dumps(best_params),
            })

        per_step_rows.append({
            "step": step_idx,
            "is_end": is_end_str,
            "oos_period": f"{oos_start_str}→{oos_end_str}",
            "best_bias_swing": best["bias_swing"],
            "best_ob_swing": best["ob_swing"],
            "best_sl_buffer": best["sl_buffer"],
            "best_min_rr": best["min_rr"],
            "is_signals": best["signals"],
            "is_decided": best["decided"],
            "is_expectancy": best["expectancy"],
            "is_avg_expectancy": round(avg_expect, 4),
            "is_best_vs_avg_ratio": (
                round(best["expectancy"] / avg_expect, 2)
                if avg_expect > 0 else None
            ),
            "oos_signals": oos_result["signals"],
            "oos_decided": oos_result["decided"],
            "oos_win_rate": oos_result["win_rate"],
            "oos_total_rr": oos_result["total_rr"],
            "oos_expectancy": oos_result["expectancy"],
        })

        elapsed_step = time.time() - ts
        print(f"[{step_idx+1}/{len(steps)}] IS→{is_end_str} | "
              f"OOS {oos_start_str}→{oos_end_str} | "
              f"best(sw={best['bias_swing']}/{best['ob_swing']},"
              f"buf={best['sl_buffer']},rr={best['min_rr']}) "
              f"IS-exp={best['expectancy']:+.3f} "
              f"(avg {avg_expect:+.3f}, ratio "
              f"{(best['expectancy']/avg_expect if avg_expect>0 else 0):.1f}×) "
              f"→ OOS: {oos_result['signals']}sig, "
              f"WR={oos_result['win_rate']:.0f}%, "
              f"R={oos_result['total_rr']:+.1f}, "
              f"exp={oos_result['expectancy']:+.3f}  "
              f"[{elapsed_step:.0f}s]")

        # ── Save checkpoint every step (allows resume after crash) ──
        if not smoke:
            try:
                # Strip non-serializable bits (signal_time is datetime → str)
                serializable_signals = []
                for s in all_oos_signals:
                    s2 = dict(s)
                    if hasattr(s2.get("signal_time"), "isoformat"):
                        s2["signal_time"] = s2["signal_time"].isoformat()
                    serializable_signals.append(s2)
                json.dump({
                    "tickers": cfg.tickers,
                    "data_start": cfg.data_start,
                    "per_step_rows": per_step_rows,
                    "all_oos_signals": serializable_signals,
                    "last_step_completed": step_idx,
                }, open(checkpoint_path, "w", encoding="utf-8"),
                    default=str, ensure_ascii=False)
            except Exception as e:
                print(f"  (checkpoint save failed: {e})")

    total_elapsed = time.time() - t0

    # ─── Save artefacts ───────────────────────────────────────────────────
    sig_df = pd.DataFrame(all_oos_signals)
    sig_df.to_csv(cfg.out_signals, index=False)

    params_df = pd.DataFrame(per_step_rows)
    params_df.to_csv(cfg.out_params, index=False)

    # ─── Summary ──────────────────────────────────────────────────────────
    total_oos_signals = len(sig_df)
    decided = sig_df["actual_rr"].notna().sum() if total_oos_signals > 0 else 0
    total_oos_rr = sig_df["actual_rr"].sum() if total_oos_signals > 0 else 0
    wins = (sig_df["outcome"] == "WIN_TP1").sum() if total_oos_signals > 0 else 0
    losses = (sig_df["outcome"] == "LOSS").sum() if total_oos_signals > 0 else 0
    wr = (wins / decided * 100) if decided > 0 else 0
    avg_oos_exp = (total_oos_rr / decided) if decided > 0 else 0

    print()
    print("=" * 78)
    print(f"WALK-FORWARD COMPLETE  ({cfg.venue}, OOS={cfg.oos_months}m)")
    print("=" * 78)
    print(f"  Total steps:                {len(steps)}")
    print(f"  Total OOS signals:          {total_oos_signals}")
    print(f"  Decided (W+L):              {decided}  ({wins}W/{losses}L)")
    print(f"  OOS win rate:               {wr:.1f}%")
    print(f"  OOS total R:                {total_oos_rr:+.2f}")
    print(f"  OOS expectancy:             {avg_oos_exp:+.4f}")
    print(f"  Elapsed:                    {total_elapsed:.1f}s "
          f"({total_elapsed/60:.1f}min)")
    print()

    # Parameter frequency (overfit indicator)
    if not params_df.empty:
        print("─" * 78)
        print("PARAMETER WIN FREQUENCY (overfit check)")
        print("─" * 78)
        for col in ["best_bias_swing", "best_ob_swing",
                    "best_sl_buffer", "best_min_rr"]:
            counts = params_df[col].value_counts()
            top = counts.index[0]
            print(f"  {col:25}: {counts.to_dict()}  "
                  f"→ most often: {top} ({counts.iloc[0]}/{len(params_df)})")

        print()
        print("─" * 78)
        print("IS vs OOS EXPECTANCY (does optimisation help?)")
        print("─" * 78)
        avg_is = params_df["is_expectancy"].mean()
        avg_oos = params_df["oos_expectancy"].mean()
        print(f"  Avg IS expectancy (best):   {avg_is:+.4f}")
        print(f"  Avg OOS expectancy:         {avg_oos:+.4f}")
        if avg_is > 0:
            print(f"  OOS/IS ratio:               {avg_oos/avg_is*100:.1f}%")

    # Write summary file
    summary = {
        "venue": cfg.venue,
        "tickers": cfg.tickers,
        "oos_months": cfg.oos_months,
        "step_months": cfg.step_months,
        "n_steps": len(steps),
        "grid_size": n_grid,
        "total_oos_signals": int(total_oos_signals),
        "decided": int(decided),
        "wins": int(wins),
        "losses": int(losses),
        "win_rate": round(wr, 2),
        "total_oos_rr": round(total_oos_rr, 2),
        "avg_oos_expectancy": round(avg_oos_exp, 4),
        "avg_is_expectancy": round(avg_is, 4) if not params_df.empty else None,
        "elapsed_seconds": round(total_elapsed, 1),
    }
    with open(cfg.out_summary, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False, default=str)

    print()
    print(f"Saved signals: {cfg.out_signals}")
    print(f"Saved params:  {cfg.out_params}")
    print(f"Saved summary: {cfg.out_summary}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", choices=["bybit", "moex"], required=True)
    ap.add_argument("--oos-months", type=int, default=1)
    ap.add_argument("--step-months", type=int, default=1)
    ap.add_argument("--min-is-trades", type=int, default=30)
    ap.add_argument("--smoke", action="store_true",
                    help="Tiny grid (9 combos), 3 tickers, for testing")
    ap.add_argument("--tickers", default=None,
                    help="Override ticker list (comma-sep)")
    ap.add_argument("--sequential", action="store_true",
                    help="Disable joblib parallelism (slower but more robust)")
    ap.add_argument("--ob-tf", default=None,
                    help="OB timeframe (default: 60M; try 4H for cleaner signals)")
    ap.add_argument("--extended", action="store_true",
                    help="Use extended grid: 27 combos (swing[5,10,15] × buf × rr)")
    args = ap.parse_args()

    if args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",")]
    elif args.smoke:
        tickers = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    elif args.venue == "bybit":
        tickers = BYBIT_TICKERS
    else:
        tickers = MOEX_TOP20

    data_start = "2023-10-25" if args.venue == "bybit" else "2023-03-01"
    data_end = "2026-07-18"
    ob_tf = args.ob_tf or "60M"
    tf_suffix = "" if ob_tf == "60M" else f"_{ob_tf}"
    ext_suffix = "_ext" if args.extended else ""
    tag = "smoke" if args.smoke else f"{args.venue}_oos{args.oos_months}m{tf_suffix}{ext_suffix}"

    cfg = WFConfig(
        venue=args.venue,
        tickers=tickers,
        data_start=data_start,
        data_end=data_end,
        step_months=args.step_months,
        oos_months=args.oos_months,
        min_is_trades=args.min_is_trades,
        out_signals=f"data/wf_{tag}_signals.csv",
        out_params=f"data/wf_{tag}_params.csv",
        out_summary=f"data/wf_{tag}_summary.json",
        ob_tf=ob_tf,
    )
    return run_walk_forward(cfg, smoke=args.smoke, extended=args.extended)


if __name__ == "__main__":
    sys.exit(main())
