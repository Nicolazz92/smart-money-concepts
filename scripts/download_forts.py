#!/usr/bin/env python3
"""Download MOEX FORTS futures, build front-month continuous contracts.

For each underlying, finds all quarterly contracts (e.g. SRZ5, SRH6, SRM6, SRU6...),
then at each date selects the contract with the highest volume (front-month) to
build a continuous OHLCV series.

Output:
    data/FUT_{BASE}_60M.csv   — 1-hour continuous
    data/FUT_{BASE}_1D.csv    — daily continuous

Usage:
    python scripts/download_forts.py                    # all 24 underlyings
    python scripts/download_forts.py --bases SR,GZ,LK   # specific
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.request
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

OUT_DIR = Path(__file__).resolve().parent.parent / "data"
MSK = ZoneInfo("Europe/Moscow")
UTC = ZoneInfo("UTC")

# FORTS contract month codes (Fung-style FORTS encoding)
# F=Jan, G=Feb, H=Mar, J=Apr, K=May, M=Jun, N=Jul, Q=Aug, U=Sep, V=Oct, X=Nov, Z=Dec
MONTH_CODE = {
    1: 'F', 2: 'G', 3: 'H', 4: 'J', 5: 'K', 6: 'M',
    7: 'N', 8: 'Q', 9: 'U', 10: 'V', 11: 'X', 12: 'Z',
}

# Quarterly expiry months for stock futures on FORTS: Mar, Jun, Sep, Dec
QUARTERS = [3, 6, 9, 12]

# Base codes → underlying name (informational)
UNDERLYINGS = {
    'Si': 'USDRUB', 'Eu': 'EURRUB', 'CR': 'CNYRUB', 'BR': 'BRENT', 'BM': 'URALS',
    'BT': 'BTC', 'GD': 'GOLD',
    'Ri': 'RTS_INDEX', 'MX': 'IMOEX',
    'SR': 'SBER', 'GZ': 'GAZP', 'LK': 'LKOH', 'MM': 'GMKN', 'RN': 'ROSN',
    'VB': 'VTBR', 'NA': 'NVTK', 'MA': 'MGNT', 'MO': 'MOEX',
    'AF': 'AFLT', 'AK': 'AFKS', 'AL': 'ALRS', 'AS': 'ASTR',
    'BN': 'BANE', 'BS': 'BSPB', 'SV': 'SNGSP',
}


def gen_contract_codes(base: str, start_year: int, end_year: int) -> list[str]:
    """Generate quarterly contract codes: SRZ5, SRH6, SRM6, SRU6...
    Year is 1-digit (e.g. SRU6 for Sep 2026)."""
    codes = []
    for y in range(start_year, end_year + 1):
        for m in QUARTERS:
            mc = MONTH_CODE[m]
            yy = y % 10  # 1 digit: 2024→4, 2025→5, 2026→6, 2027→7
            codes.append(f"{base}{mc}{yy}")
    return codes


def iss_get(url: str, retries: int = 3) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"ISS GET failed: {last_err}\nURL: {url}")


def fetch_daily_for_contract(secid: str, start: str, end: str) -> pd.DataFrame:
    """Fetch daily OHLCV for one contract. Returns DataFrame indexed by UTC datetime."""
    all_rows = []
    cursor = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    while cursor <= end_dt:
        win_end = min(cursor + timedelta(days=200), end_dt)
        url = (
            f"https://iss.moex.com/iss/history/engines/futures/markets/forts/"
            f"securities/{secid}.json"
            f"?from={cursor.strftime('%Y-%m-%d')}&till={win_end.strftime('%Y-%m-%d')}"
            f"&iss.meta=off"
        )
        d = iss_get(url)
        h = d.get("history", {})
        cols = h.get("columns", [])
        rows = h.get("data", [])
        for row in rows:
            if row is None:
                continue
            rec = dict(zip(cols, row))
            td = rec.get("TRADEDATE")
            if not td:
                continue
            vol = rec.get("VOLUME") or rec.get("NUMTRADES") or 0
            val = rec.get("VALUE") or 0
            o = rec.get("OPEN")
            c = rec.get("CLOSE") or rec.get("SETTLEPRICE")
            if o is None or c is None:
                continue
            all_rows.append({
                "date": td,
                "open": float(o),
                "high": float(rec.get("HIGH") or o),
                "low": float(rec.get("LOW") or o),
                "close": float(c),
                "volume": float(vol),
                "turnover": float(val),
                "contract": secid,
            })
        cursor = win_end + timedelta(days=1)
        time.sleep(0.3)
    if not all_rows:
        return pd.DataFrame()
    df = pd.DataFrame(all_rows)
    df["time"] = pd.to_datetime(df["date"]).dt.tz_localize(MSK).dt.tz_convert(UTC)
    df = df.set_index("time").sort_index()
    return df


def build_front_month_daily(all_contracts: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """At each date, pick the contract with highest volume. Build continuous series."""
    # Combine all
    pieces = []
    for secid, df in all_contracts.items():
        if df.empty:
            continue
        d = df.copy()
        d["contract"] = secid
        pieces.append(d)
    if not pieces:
        return pd.DataFrame()
    big = pd.concat(pieces).sort_index()
    # For each date, keep row with max volume
    big["vol_rank"] = big.groupby(big.index)["volume"].rank(ascending=False, method="first")
    fm = big[big["vol_rank"] == 1.0].copy()
    fm = fm.drop(columns=["vol_rank"])
    return fm[["open", "high", "low", "close", "volume", "turnover", "contract"]]


def fetch_intraday_60m_continuous(
    base: str, front_month: pd.DataFrame, start: str, end: str
) -> pd.DataFrame:
    """For each segment (period when a single contract was front-month),
    fetch 1h candles for that contract, then concat."""
    if front_month.empty:
        return pd.DataFrame()
    # Group by contract, find date ranges
    fm = front_month.reset_index()
    fm["group"] = (fm["contract"] != fm["contract"].shift()).cumsum()
    pieces = []
    for _, g in fm.groupby("group"):
        contract = g["contract"].iloc[0]
        seg_start = g["time"].min().strftime("%Y-%m-%d")
        seg_end = g["time"].max().strftime("%Y-%m-%d")
        # Fetch 1h for this contract in this window
        all_rows = []
        cursor = datetime.strptime(seg_start, "%Y-%m-%d")
        end_dt = datetime.strptime(seg_end, "%Y-%m-%d")
        while cursor <= end_dt:
            win_end = min(cursor + timedelta(days=25), end_dt)
            url = (
                f"https://iss.moex.com/iss/engines/futures/markets/forts/"
                f"securities/{contract}/candles.json"
                f"?from={cursor.strftime('%Y-%m-%d')}&till={win_end.strftime('%Y-%m-%d')}"
                f"&interval=60&iss.meta=off"
            )
            try:
                d = iss_get(url)
            except Exception as e:
                print(f"    1h fetch {contract} {cursor.date()}: {e}")
                cursor = win_end + timedelta(days=1)
                continue
            candles = d.get("candles", {})
            cols = candles.get("columns", [])
            rows = candles.get("data", [])
            for row in rows:
                rec = dict(zip(cols, row))
                begin = rec.get("begin")
                if not begin:
                    continue
                all_rows.append({
                    "ts_msk": begin,
                    "open": float(rec["open"]),
                    "high": float(rec["high"]),
                    "low": float(rec["low"]),
                    "close": float(rec["close"]),
                    "volume": float(rec.get("volume", 0) or 0),
                })
            cursor = win_end + timedelta(days=1)
            time.sleep(0.2)
        if not all_rows:
            continue
        seg_df = pd.DataFrame(all_rows)
        seg_df["time"] = pd.to_datetime(seg_df["ts_msk"]).dt.tz_localize(MSK).dt.tz_convert(UTC)
        seg_df = seg_df.set_index("time").sort_index()
        pieces.append(seg_df[["open", "high", "low", "close", "volume"]])
    if not pieces:
        return pd.DataFrame()
    return pd.concat(pieces).sort_index()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bases", default=None,
                    help="Comma-separated futures bases (e.g. SR,GZ,LK). Default: all")
    ap.add_argument("--data-dir", default=str(OUT_DIR))
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default="2026-07-18")
    ap.add_argument("--timeframes", default="1D,60M")
    ap.add_argument("--skip-existing", action="store_true")
    args = ap.parse_args()

    bases = args.bases.split(",") if args.bases else list(UNDERLYINGS.keys())
    bases = [b.strip() for b in bases]
    start_year = int(args.start[:4])
    end_year = int(args.end[:4])
    tfs = [t.strip().upper() for t in args.timeframes.split(",")]
    out_dir = Path(args.data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Downloading FORTS futures for {len(bases)} bases")
    print(f"Period: {args.start} → {args.end}")
    print(f"Timeframes: {tfs}")
    print()

    ok, failed = [], []
    for i, base in enumerate(bases, 1):
        und_name = UNDERLYINGS.get(base, "?")
        print(f"[{i}/{len(bases)}] {base} ({und_name})")

        # Check skip
        if args.skip_existing and all(
            (out_dir / f"FUT_{base}_{tf}.csv").exists() for tf in tfs
        ):
            print(f"  SKIP (exists)")
            continue

        # Generate contract codes for this base
        codes = gen_contract_codes(base, start_year, end_year)
        print(f"  {len(codes)} possible contracts: {codes[0]}, ..., {codes[-1]}")

        # Fetch daily for each
        daily_contracts = {}
        for code in codes:
            try:
                df = fetch_daily_for_contract(code, args.start, args.end)
                if not df.empty:
                    daily_contracts[code] = df
                    print(f"  {code}: {len(df):>4} daily bars")
            except Exception as e:
                print(f"  {code}: error {e}")
            time.sleep(0.15)

        if not daily_contracts:
            print(f"  ⚠ no data — skipping")
            failed.append(base)
            continue

        # Build front-month continuous daily
        fm_daily = build_front_month_daily(daily_contracts)
        if fm_daily.empty:
            print(f"  ⚠ empty front-month — skipping")
            failed.append(base)
            continue

        # Save daily
        if "1D" in tfs:
            out_path = out_dir / f"FUT_{base}_1D.csv"
            fm_daily.drop(columns=["contract", "turnover"]).to_csv(out_path)
            print(f"  1D: {len(fm_daily):>4} bars → {out_path.name} "
                  f"({fm_daily.index[0].date()} → {fm_daily.index[-1].date()})")

        # Fetch 1h continuous (for each segment, fetch from FORTS candle endpoint)
        if "60M" in tfs:
            print(f"  fetching 60M for front-month segments...")
            fm_60m = fetch_intraday_60m_continuous(base, fm_daily, args.start, args.end)
            if not fm_60m.empty:
                out_path = out_dir / f"FUT_{base}_60M.csv"
                fm_60m.to_csv(out_path)
                print(f"  60M: {len(fm_60m):>5} bars → {out_path.name} "
                      f"({fm_60m.index[0].date()} → {fm_60m.index[-1].date()})")
            else:
                print(f"  ⚠ no 60M data")

        ok.append(base)
        print()

    print("=" * 60)
    print(f"DONE  ok={len(ok)}  failed={len(failed)}")
    if failed:
        print(f"Failed: {', '.join(failed)}")
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
