#!/usr/bin/env python3
"""Generate 4H timeframe by resampling 60M data.

MOEX ISS API doesn't natively support 4H candles, but we have 1H CSVs
already. Resample them to 4H with proper OHLCV aggregation.

For Bybit we can directly download 4H (interval=240), so this script
is MOEX-only. Use --venue bybit to download directly via download_bybit.py.

Usage:
    python scripts/resample_to_4h.py --venue moex
    python scripts/resample_to_4h.py --venue moex --tickers SBER,GAZP,LKOH
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd


MOEX_TOP50 = [
    "GAZP", "SBER", "GMKN", "ROSN", "LKOH", "NVTK", "TATN", "MTSS",
    "MGNT", "MOEX", "NLMK", "MAGN", "ALRS", "CHMF", "PHOR", "AFKS",
    "PLZL", "SNGSP", "SNGS", "T", "OZON", "POSI", "TRNFP", "RUAL",
    "X5", "CBOM", "BSPB", "AFLT", "HEAD", "SFIN", "IRAO", "FEES",
    "HYDR", "RTKM", "LSNGP", "TGKA", "PIKK", "MTLR", "BANEP", "SMLT",
    "VTBR", "TATNP", "SBERP", "MSNG", "SIBN", "AKRN", "SELG", "FESH",
    "ETLN", "MTSS",
]


def resample_4h(df_1h: pd.DataFrame) -> pd.DataFrame:
    """Resample 1H OHLCV to 4H.

    Uses 4-hour bins starting at midnight UTC (00:00, 04:00, 08:00...).
    For MOEX trading hours (07:00-19:00 MSK = 04:00-16:00 UTC), this
    produces ~3 bins per day of trading data.
    """
    if df_1h.empty:
        return df_1h
    return df_1h.resample("4h", label="left", closed="left").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna(subset=["open"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", choices=["moex"], default="moex",
                    help="For MOEX we resample from 60M. Bybit uses download_bybit.py.")
    ap.add_argument("--tickers", default=None,
                    help="Comma-separated tickers. Default: top-50 MOEX, or ALL if --all.")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--all", action="store_true",
                    help="Resample ALL tickers with source-TF CSVs in data-dir.")
    ap.add_argument("--source-tf", default="60M",
                    help="Source timeframe to resample from (default 60M)")
    args = ap.parse_args()

    if args.all:
        # Find all {TICKER}_60M.csv files in data-dir
        data_dir = Path(args.data_dir)
        tickers = sorted([
            p.stem.replace(f"_{args.source_tf}", "")
            for p in data_dir.glob(f"*_{args.source_tf}.csv")
        ])
        print(f"Found {len(tickers)} tickers with {args.source_tf} data")
    else:
        tickers = (args.tickers.split(",") if args.tickers else MOEX_TOP50)
        tickers = [t.strip().upper() for t in tickers]

    data_dir = Path(args.data_dir)
    ok, skipped, failed = [], [], []

    for i, tkr in enumerate(tickers, 1):
        src = data_dir / f"{tkr}_{args.source_tf}.csv"
        dst = data_dir / f"{tkr}_4H.csv"
        if not src.exists():
            print(f"[{i}/{len(tickers)}] {tkr}: no {args.source_tf} source")
            failed.append(tkr)
            continue
        if dst.exists():
            print(f"[{i}/{len(tickers)}] {tkr}: 4H exists, skip")
            skipped.append(tkr)
            continue
        try:
            df = pd.read_csv(src, parse_dates=["time"]).set_index("time")
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            df4 = resample_4h(df)
            df4.to_csv(dst)
            print(f"[{i}/{len(tickers)}] {tkr}: {len(df):>5} 1H bars → "
                  f"{len(df4):>5} 4H bars ({df4.index[0].date()} → {df4.index[-1].date()})")
            ok.append(tkr)
        except Exception as e:
            print(f"[{i}/{len(tickers)}] {tkr}: ERROR {e}")
            failed.append(tkr)

    print(f"\nDONE ok={len(ok)} skipped={len(skipped)} failed={len(failed)}")
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
