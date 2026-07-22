#!/usr/bin/env python3
"""Download Bybit historical OHLCV via V5 public API (no API key needed).

Endpoints:
  klines : GET /v5/market/kline?category={linear|spot}&symbol=...&interval=...&start=...&end=...
           Returns up to 1000 candles per call, newest-first.
           Intervals: 1,3,5,15,30,60,120,240,360,720,D,W,M (minutes / day / week / month)
  tickers: GET /v5/market/tickers?category=... (24h turnover for ranking — run separately)

History depth:
  - Majors (BTC/ETH/SOL/...): ~2.7 years of 1h, full D
  - 1000 candle limit per call -> paginate backward in time using `end` cursor
  - All timestamps are UTC milliseconds

Output files (per symbol, suffix avoids clash with MOEX tickers like SBER):
  data/{SYMBOL}_bybit_60M.csv
  data/{SYMBOL}_bybit_1D.csv

Usage:
    python scripts/download_bybit.py --list data/_bybit_final.json
    python scripts/download_bybit.py --list data/_bybit_final.json --start 2024-01-01
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

OUT_DIR = Path(__file__).resolve().parent.parent / "data"

UA = {"User-Agent": "Mozilla/5.0"}
INTERVAL_MIN = {
    "15M": "15",
    "30M": "30",
    "60M": "60",
    "4H": "240",
    "1D": "D",
    "1W": "W",
}


def iss_get(url: str, retries: int = 3) -> dict:
    req = urllib.request.Request(url, headers=UA)
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Bybit GET failed after {retries} retries: {last}\nURL: {url}")


def fetch_klines(symbol: str, category: str, interval_code: str,
                 start_ms: int, end_ms: int) -> pd.DataFrame:
    """Paginate backward in time. Bybit returns newest-first, max 1000 per call.

    Returns DataFrame indexed by UTC datetime with open/high/low/close/volume columns.
    Volume unit: base coin (perp) or base coin (spot).
    """
    all_rows = []
    cursor_end = end_ms  # paginate by moving cursor_end backward
    while cursor_end > start_ms:
        url = (
            f"https://api.bybit.com/v5/market/kline"
            f"?category={category}&symbol={symbol}&interval={interval_code}"
            f"&start={start_ms}&end={cursor_end}&limit=1000"
        )
        d = iss_get(url)
        kl = d.get("result", {}).get("list", [])
        if not kl:
            break
        # Each row: [start(ms), open, high, low, close, volume, turnover]
        for row in kl:
            ts_ms = int(row[0])
            if ts_ms < start_ms or ts_ms > cursor_end:
                continue
            all_rows.append({
                "ts_ms": ts_ms,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]) if row[5] not in (None, "") else 0.0,
            })
        # Move cursor to the oldest timestamp fetched
        oldest = min(int(r[0]) for r in kl)
        if oldest >= cursor_end:  # no progress
            break
        cursor_end = oldest - 1
        time.sleep(0.12)  # ~8 rps — well under public limit
        if len(kl) < 1000:
            break

    if not all_rows:
        return pd.DataFrame()
    df = pd.DataFrame(all_rows).drop_duplicates(subset="ts_ms")
    df["time"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    df = df.set_index("time").sort_index()
    return df[["open", "high", "low", "close", "volume"]]


def main() -> int:
    ap = argparse.ArgumentParser(description="Download Bybit historical OHLCV.")
    ap.add_argument("--list", required=True, help="JSON file: array of {symbol,category,earliest}")
    ap.add_argument("--data-dir", default=str(OUT_DIR))
    ap.add_argument("--start", default="2023-10-25",
                    help="ISO date. Default = first available date for majors.")
    ap.add_argument("--end", default="2026-07-20")
    ap.add_argument("--timeframes", default="60M,1D",
                    help="Comma-separated, options: 60M,1D")
    ap.add_argument("--skip-existing", action="store_true")
    ap.add_argument("--only", default=None, help="Comma-separated subset to download.")
    args = ap.parse_args()

    out_dir = Path(args.data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    instruments = json.loads(Path(args.list).read_text(encoding="utf-8"))
    if args.only:
        keep = {s.strip().upper() for s in args.only.split(",") if s.strip()}
        instruments = [i for i in instruments if i["symbol"].upper() in keep]

    tfs = [t.strip().upper() for t in args.timeframes.split(",")]
    start_ms = int(datetime.strptime(args.start, "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime.strptime(args.end, "%Y-%m-%d")
                 .replace(hour=23, minute=59, tzinfo=timezone.utc).timestamp() * 1000)

    print(f"Downloading Bybit data ({args.start} → {args.end})")
    print(f"Timeframes: {tfs}    Instruments: {len(instruments)}\n")

    ok, failed, skipped = [], [], []
    for i, inst in enumerate(instruments, 1):
        sym = inst["symbol"]
        cat = inst.get("category", "linear")
        prefix = f"[{i}/{len(instruments)}]"
        # Skip if earliest known history is later than our window start
        earliest = inst.get("earliest")
        if earliest and earliest > args.start:
            local_start_ms = int(datetime.strptime(earliest, "%Y-%m-%d")
                                 .replace(tzinfo=timezone.utc).timestamp() * 1000)
        else:
            local_start_ms = start_ms

        for tf in tfs:
            out_path = out_dir / f"{sym}_bybit_{tf}.csv"
            if args.skip_existing and out_path.exists():
                print(f"{prefix} {sym} {tf}: SKIP (exists)")
                skipped.append(f"{sym}_{tf}")
                continue
            try:
                df = fetch_klines(sym, cat, INTERVAL_MIN[tf], local_start_ms, end_ms)
                if df.empty:
                    print(f"{prefix} {sym} {tf}: no data")
                    failed.append(f"{sym}_{tf}")
                    continue
                df.to_csv(out_path)
                print(f"{prefix} {sym} {cat}/{tf}: {len(df):>5} bars "
                      f"({df.index[0].date()} → {df.index[-1].date()})")
                ok.append(f"{sym}_{tf}")
            except Exception as e:
                print(f"{prefix} {sym} {tf}: ERROR {e}")
                failed.append(f"{sym}_{tf}")

    print("\n" + "=" * 60)
    print(f"DONE  ok={len(ok)}  skipped={len(skipped)}  failed={len(failed)}")
    if failed:
        print("Failed:", ", ".join(failed))
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
