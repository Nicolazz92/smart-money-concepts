#!/usr/bin/env python3
"""Download XAUUSD historical data via yfinance and save as CSV.

yfinance intraday limits (as of 2026):
  - 1d: unlimited history (3+ years OK)
  - 1h, 4h: last 730 days (~2 years) only
  - 30m: last 60 days only  -> too short for backtest

Strategy: download daily for bias + 4h for OB. This gives ~2 years of
overlapping history — enough for a first meaningful backtest.

The SMC strategy expects OHLCV with columns: open, high, low, close, volume,
indexed by timezone-aware datetime (UTC).

Usage:
    python scripts/download_xauusd.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

# GC=F = gold futures front-month. XAUUSD=F was delisted on Yahoo.
# GLD is the SPDR ETF — close substitute, but GC=F tracks spot better.
TICKER = "GC=F"
OUT_DIR = Path(__file__).resolve().parent.parent / "data"

# Period that maximises overlap between daily (3y) and 4h (730d).
START = "2023-01-01"
END = "2026-07-21"


def fetch(interval: str, period: str | None = None) -> pd.DataFrame:
    """Fetch OHLCV and normalise to the format the SMC code expects.

    Returns DataFrame with:
      - tz-aware UTC DatetimeIndex named 'time'
      - columns: open, high, low, close, volume (lowercase)
    """
    if period:
        raw = yf.download(
            TICKER, period=period, interval=interval,
            progress=False, auto_adjust=True,
        )
    else:
        raw = yf.download(
            TICKER, start=START, end=END, interval=interval,
            progress=False, auto_adjust=True,
        )

    if raw is None or raw.empty:
        raise RuntimeError(f"No data returned for {TICKER} {interval}")

    # yfinance returns MultiIndex columns when a single ticker is requested
    # in newer versions — flatten to simple columns.
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    df = raw.rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume",
    })[["open", "high", "low", "close", "volume"]].copy()

    # Normalise timezone:
    # - daily data is tz-naive -> localise as UTC at midnight
    # - intraday is tz-aware (America/New_York) -> convert to UTC
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    df.index.name = "time"
    # Drop rows with NaN in core OHLC (rare holes)
    df = df.dropna(subset=["open", "high", "low", "close"])
    return df


def report_gaps(df: pd.DataFrame, expected_minutes: int) -> None:
    """Report obvious data gaps (longer than 2× the expected spacing)."""
    if len(df) < 2:
        return
    diffs = df.index.to_series().diff().dt.total_seconds() / 60
    expected = expected_minutes
    big_gaps = diffs[diffs > expected * 3].dropna()
    if len(big_gaps) > 0:
        print(f"  ⚠ {len(big_gaps)} gaps > 3× expected ({expected}m):")
        for ts, gap in big_gaps.head(5).items():
            print(f"      {ts} -> gap {gap/60:.1f}h")
        if len(big_gaps) > 5:
            print(f"      ... and {len(big_gaps)-5} more")
    else:
        print(f"  ✓ no large gaps detected")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Downloading {TICKER} from yfinance...")
    print(f"Output dir: {OUT_DIR}\n")

    # Daily: 3+ years for bias
    print("=== Daily (bias timeframe) ===")
    daily = fetch("1d")
    daily_path = OUT_DIR / "XAUUSD_1D.csv"
    daily.to_csv(daily_path)
    print(f"  saved {len(daily)} bars -> {daily_path.name}")
    print(f"  range: {daily.index[0]} → {daily.index[-1]}")
    report_gaps(daily, expected_minutes=1440)
    print()

    # 4-hour: last 730 days for OB
    print("=== 4-hour (OB timeframe — 730-day yfinance limit) ===")
    h4 = fetch("4h", period="730d")
    h4_path = OUT_DIR / "XAUUSD_4H.csv"
    h4.to_csv(h4_path)
    print(f"  saved {len(h4)} bars -> {h4_path.name}")
    print(f"  range: {h4.index[0]} → {h4.index[-1]}")
    report_gaps(h4, expected_minutes=240)
    print()

    # 1-hour: also fetch (in case we want finer OB TF)
    print("=== 1-hour (alternative OB timeframe) ===")
    h1 = fetch("1h", period="730d")
    h1_path = OUT_DIR / "XAUUSD_1H.csv"
    h1.to_csv(h1_path)
    print(f"  saved {len(h1)} bars -> {h1_path.name}")
    print(f"  range: {h1.index[0]} → {h1.index[-1]}")
    report_gaps(h1, expected_minutes=60)
    print()

    # 30-minute: last 60 days only — short, but still save for live testing
    print("=== 30-minute (only 60 days available — short) ===")
    m30 = fetch("30m", period="60d")
    m30_path = OUT_DIR / "XAUUSD_30M.csv"
    m30.to_csv(m30_path)
    print(f"  saved {len(m30)} bars -> {m30_path.name}")
    print(f"  range: {m30.index[0]} → {m30.index[-1]}")
    report_gaps(m30, expected_minutes=30)
    print()

    # Summary
    print("=" * 60)
    print("DOWNLOAD COMPLETE")
    print("=" * 60)
    print(f"Recommended backtest config: bias=1D, OB=4H")
    overlap_start = max(daily.index[0], h4.index[0])
    print(f"Backtest can start from: {overlap_start.date()}")
    print(f"Backtest end: {h4.index[-1].date()}")
    print(f"Duration: {(h4.index[-1] - overlap_start).days} days "
          f"({(h4.index[-1] - overlap_start).days/365.25:.2f} years)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
