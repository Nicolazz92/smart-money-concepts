"""CSV-based OHLCV data provider for offline backtesting.

Reads from `{data_dir}/{SYMBOL}_{TIMEFRAME}.csv` (e.g. data/XAUUSD_4H.csv).
Files must have:
  - timezone-aware ISO timestamps in the first column named 'time'
  - lowercase columns: open, high, low, close, volume

This provider enables running BacktestEngine without MT5 / OANDA. It does
NOT support live mode (no recent-data refresh). For live signal bot, use
MT5Provider or OandaProvider from the original code.

Usage:
    provider = CsvProvider(data_dir="data")
    df = provider.get_ohlcv_range("XAUUSD", "4H", from_dt, to_dt)
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from .base import DataProvider

logger = logging.getLogger(__name__)


class CsvProvider(DataProvider):
    """Offline OHLCV provider backed by pre-downloaded CSV files.

    CSVs are loaded lazily on first access and cached in memory — the
    backtest engine calls get_ohlcv_range() many times, so we avoid
    re-parsing the file on every call.
    """

    def __init__(self, data_dir: str | Path = "data", filename_suffix: str = ""):
        """Initialise provider.

        filename_suffix : inserted between symbol and timeframe in the file
                          name, e.g. "_bybit" -> "BTCUSDT_bybit_60M.csv".
                          Default "" matches MOEX/xauusd convention.
        """
        self.data_dir = Path(data_dir)
        self.filename_suffix = filename_suffix
        # Cache: (symbol, timeframe) -> DataFrame (full, tz-aware UTC)
        self._cache: dict[tuple[str, str], pd.DataFrame] = {}

    # ------------------------------------------------------------------
    # DataProvider interface
    # ------------------------------------------------------------------
    def connect(self) -> bool:
        """No-op for CSV provider. Returns True if data_dir exists."""
        if not self.data_dir.exists():
            logger.error("Data dir does not exist: %s", self.data_dir)
            return False
        logger.info("CsvProvider ready, data_dir=%s", self.data_dir)
        return True

    def disconnect(self) -> None:
        """No-op."""
        self._cache.clear()

    def get_ohlcv(
        self, symbol: str, timeframe: str, count: int = 200
    ) -> pd.DataFrame:
        """Return the last *count* candles for the symbol/timeframe."""
        full = self._load(symbol, timeframe)
        if full.empty:
            return full
        return full.iloc[-count:].copy()

    def get_ohlcv_range(
        self,
        symbol: str,
        timeframe: str,
        from_dt: datetime,
        to_dt: datetime,
    ) -> pd.DataFrame:
        """Return candles in [from_dt, to_dt]. Both endpoints inclusive."""
        full = self._load(symbol, timeframe)
        if full.empty:
            return full

        # Normalise timezone on the query bounds so comparison works regardless
        # of whether the caller passed tz-aware or tz-naive datetimes.
        from_ts = self._normalise_ts(from_dt, ref=full.index)
        to_ts = self._normalise_ts(to_dt, ref=full.index)

        mask = (full.index >= from_ts) & (full.index <= to_ts)
        return full.loc[mask].copy()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _load(self, symbol: str, timeframe: str) -> pd.DataFrame:
        key = (symbol, timeframe)
        if key in self._cache:
            return self._cache[key]

        path = self.data_dir / f"{symbol}{self.filename_suffix}_{timeframe}.csv"
        if not path.exists():
            logger.error(
                "CSV not found: %s. Run scripts/download_xauusd.py first.", path
            )
            self._cache[key] = pd.DataFrame()
            return self._cache[key]

        df = pd.read_csv(path, parse_dates=["time"])
        df = df.set_index("time")
        # Defensive: ensure index is tz-aware UTC
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")
        df = df.sort_index()
        # Drop duplicate timestamps if any (keep last)
        df = df[~df.index.duplicated(keep="last")]

        self._cache[key] = df
        logger.info(
            "Loaded %s %s: %d bars, %s → %s",
            symbol, timeframe, len(df),
            df.index[0].isoformat(), df.index[-1].isoformat(),
        )
        return df

    @staticmethod
    def _normalise_ts(ts: datetime, ref: pd.DatetimeIndex) -> datetime:
        """Make *ts* comparable to *ref* (tz-aware UTC index)."""
        if ref.tz is None:
            # Reference is naive — strip tz from ts if present
            if ts.tzinfo is not None:
                return ts.replace(tzinfo=None)
            return ts
        # Reference is tz-aware — ensure ts is too
        if ts.tzinfo is None:
            return ts.replace(tzinfo=ref.tz)
        return ts.astimezone(ref.tz)
