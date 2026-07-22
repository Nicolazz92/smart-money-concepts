#!/usr/bin/env python3
"""Download MOEX historical OHLCV data via ISS API (free, no key).

MOEX ISS endpoints used:
  Daily (shares): /iss/history/engines/stock/markets/shares/boards/TQBR/securities/{SEC}.json
  Daily (index) : /iss/history/engines/stock/markets/index/securities/IMOEX.json
  Intraday 1h   : /iss/engines/stock/markets/{shares|index}/securities/{SEC}/candles.json?interval=60

Key facts discovered during testing:
  - Daily history depth: 10+ years for top tickers (since 2013 on TQBR)
  - 1-hour candles: 3+ years depth (verified back to 2022-07)
  - ISS returns max 500 rows per request -> must paginate by date window
  - Timestamps are in Moscow time (MSK = Europe/Moscow = UTC+3)
  - Shares candle cols: [open, close, high, low, value, volume, begin, end]
  - Daily TQBR cols:     [... TRADEDATE, ..., OPEN, LOW, HIGH, LEGALCLOSEPRICE, CLOSE]
  - IMOEX cols differ slightly (CLOSE before OPEN)

Output files (per ticker):
  data/{SEC}_1D.csv  — daily OHLCV
  data/{SEC}_60M.csv — 1-hour OHLCV

All timestamps are converted to UTC for consistency with the SMC strategy code.
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.parse
import urllib.request
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

OUT_DIR = Path(__file__).resolve().parent.parent / "data"
MSK = ZoneInfo("Europe/Moscow")
UTC = ZoneInfo("UTC")

# Top-5 MOEX liquid shares + IMOEX index (default — original preset)
DEFAULT_SECURITIES = [
    ("GAZP", "shares", "TQBR"),
    ("SBER", "shares", "TQBR"),
    ("GMKN", "shares", "TQBR"),
    ("ROSN", "shares", "TQBR"),
    ("LKOH", "shares", "TQBR"),
    ("IMOEX", "index", "SNDX"),
]

# Index instruments that trade on the SNDX board, not TQBR.
# Anything else from the list file is treated as a TQBR share.
INDEX_SECURITIES = {"IMOEX", "RTSI", "RTSSTD", "MOEXMM", "MOEXOG"}

START = "2023-01-01"
END = "2026-07-21"


def load_securities(list_path: str | None) -> list[tuple[str, str, str]]:
    """Build the (secid, market, board) list.

    - If list_path is None: use DEFAULT_SECURITIES.
    - Else: read a JSON array of SECIDs from list_path. Each becomes a
      TQBR share unless present in INDEX_SECURITIES (then index/SNDX).
      IMOEX is appended automatically unless already present.
    """
    if list_path is None:
        return list(DEFAULT_SECURITIES)
    text = Path(list_path).read_text(encoding="utf-8")
    secids = json.loads(text)
    out: list[tuple[str, str, str]] = []
    for s in secids:
        s = str(s).strip().upper()
        if not s:
            continue
        if s in INDEX_SECURITIES:
            out.append((s, "index", "SNDX"))
        else:
            out.append((s, "shares", "TQBR"))
    # Ensure IMOEX present (useful benchmark even if not in user list)
    if not any(s == "IMOEX" for s, _, _ in out):
        out.append(("IMOEX", "index", "SNDX"))
    return out

# Be polite to ISS — they don't enforce rate limits hard but ask for ~10 rps
SLEEP_BETWEEN_REQUESTS = 0.3


def iss_get(url: str, retries: int = 3) -> dict:
    """GET a JSON object from ISS with retry/backoff."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"ISS GET failed after {retries} retries: {last_err}\nURL: {url}")


def fetch_daily(sec: str, market: str, board: str, start: str, end: str) -> pd.DataFrame:
    """Fetch daily OHLCV. Returns DataFrame indexed by UTC midnight."""
    # Daily endpoint supports date range directly; paginate by ~6 months
    # to stay well under the 500-row limit (≈250 trading days/year).
    all_rows: list[dict] = []
    win_start = datetime.strptime(start, "%Y-%m-%d")
    win_end = datetime.strptime(end, "%Y-%m-%d")
    cursor = win_start
    while cursor <= win_end:
        win_to = min(cursor + timedelta(days=200), win_end)
        url = (
            f"https://iss.moex.com/iss/history/engines/stock/markets/{market}"
            f"/boards/{board}/securities/{sec}.json"
            f"?from={cursor.strftime('%Y-%m-%d')}&till={win_to.strftime('%Y-%m-%d')}"
        )
        d = iss_get(url)
        history = d.get("history", {})
        cols = history.get("columns", [])
        rows = history.get("data", [])
        for row in rows:
            if row is None:
                continue
            rec = dict(zip(cols, row))
            # Skip rows from other boards (TQBR query can return TQTF etc.)
            if rec.get("BOARDID") != board:
                continue
            tradedate = rec.get("TRADEDATE")
            if not tradedate:
                continue
            open_ = rec.get("OPEN")
            high = rec.get("HIGH")
            low = rec.get("LOW")
            close = rec.get("LEGALCLOSEPRICE") or rec.get("CLOSE")
            vol = rec.get("VOLUME") or rec.get("NUMTRADES") or 0
            if open_ is None or close is None:
                continue
            all_rows.append({
                "date": tradedate,
                "open": float(open_),
                "high": float(high) if high else float(open_),
                "low": float(low) if low else float(open_),
                "close": float(close),
                "volume": float(vol) if vol else 0.0,
            })
        cursor = win_to + timedelta(days=1)
        time.sleep(SLEEP_BETWEEN_REQUESTS)

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df["time"] = pd.to_datetime(df["date"]).dt.tz_localize(MSK).dt.tz_convert(UTC)
    df = df.set_index("time").sort_index()
    df = df[["open", "high", "low", "close", "volume"]]
    df = df[~df.index.duplicated(keep="last")]
    return df


def fetch_intraday_1h(sec: str, market: str, start: str, end: str) -> pd.DataFrame:
    """Fetch 1-hour candles. Returns DataFrame indexed by UTC datetime.

    ISS returns max 500 rows per call (~ 1 month of 1h candles), so we
    paginate by 25-day windows.
    """
    all_rows: list[dict] = []
    win_start = datetime.strptime(start, "%Y-%m-%d")
    win_end = datetime.strptime(end, "%Y-%m-%d")
    cursor = win_start
    while cursor <= win_end:
        win_to = min(cursor + timedelta(days=25), win_end)
        url = (
            f"https://iss.moex.com/iss/engines/stock/markets/{market}"
            f"/securities/{sec}/candles.json"
            f"?from={cursor.strftime('%Y-%m-%d')}&till={win_to.strftime('%Y-%m-%d')}"
            f"&interval=60"
        )
        d = iss_get(url)
        candles = d.get("candles", {})
        cols = candles.get("columns", [])
        rows = candles.get("data", [])
        for row in rows:
            rec = dict(zip(cols, row))
            begin = rec.get("begin")  # '2024-01-15 10:00:00' MSK
            if not begin:
                continue
            open_ = rec.get("open")
            close = rec.get("close")
            high = rec.get("high")
            low = rec.get("low")
            volume = rec.get("volume", 0) or 0
            if open_ is None or close is None:
                continue
            all_rows.append({
                "ts_msk": begin,
                "open": float(open_),
                "high": float(high) if high else float(open_),
                "low": float(low) if low else float(open_),
                "close": float(close),
                "volume": float(volume),
            })
        cursor = win_to + timedelta(days=1)
        time.sleep(SLEEP_BETWEEN_REQUESTS)

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df["time"] = pd.to_datetime(df["ts_msk"]).dt.tz_localize(MSK).dt.tz_convert(UTC)
    df = df.set_index("time").sort_index()
    df = df[["open", "high", "low", "close", "volume"]]
    df = df[~df.index.duplicated(keep="last")]
    return df


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Download MOEX historical OHLCV via ISS API."
    )
    ap.add_argument(
        "--list", default=None,
        help="Path to JSON file with array of SECIDs to download "
             "(default: built-in top-5 + IMOEX).",
    )
    ap.add_argument("--start", default=START)
    ap.add_argument("--end", default=END)
    ap.add_argument(
        "--data-dir", default=str(OUT_DIR),
        help="Output directory (default: ../data)",
    )
    ap.add_argument(
        "--skip-existing", action="store_true",
        help="Skip tickers whose {SEC}_1D.csv already exists.",
    )
    ap.add_argument(
        "--only", default=None,
        help="Comma-separated subset to download (e.g. SBER,GAZP).",
    )
    args = ap.parse_args()

    out_dir = Path(args.data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    securities = load_securities(args.list)
    if args.only:
        keep = {s.strip().upper() for s in args.only.split(",") if s.strip()}
        securities = [s for s in securities if s[0] in keep]

    print(f"Downloading MOEX data ({args.start} → {args.end})")
    print(f"Output dir: {out_dir}")
    print(f"Securities: {len(securities)}\n")

    ok, skipped, failed = [], [], []
    for i, (sec, market, board) in enumerate(securities, 1):
        prefix = f"[{i}/{len(securities)}]"
        daily_path = out_dir / f"{sec}_1D.csv"
        if args.skip_existing and daily_path.exists():
            print(f"{prefix} {sec}: SKIP (exists)")
            skipped.append(sec)
            continue
        try:
            daily = fetch_daily(sec, market, board, args.start, args.end)
            if daily.empty:
                print(f"{prefix} {sec} ({market}/{board}): ⚠ no daily data")
                failed.append(sec)
                continue
            daily.to_csv(daily_path)
            intraday = fetch_intraday_1h(sec, market, args.start, args.end)
            if not intraday.empty:
                intraday.to_csv(out_dir / f"{sec}_60M.csv")
            print(
                f"{prefix} {sec} ({market}/{board}): "
                f"daily {len(daily):>5} bars"
                + (f" | 1h {len(intraday):>5} bars" if not intraday.empty else "")
            )
            ok.append(sec)
        except Exception as e:
            print(f"{prefix} {sec}: ERROR {e}")
            failed.append(sec)

    print("\n" + "=" * 60)
    print(f"DONE  ok={len(ok)}  skipped={len(skipped)}  failed={len(failed)}")
    if failed:
        print("Failed:", ", ".join(failed))
    print("=" * 60)
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
