"""MOEX data provider for FORTS futures and TQBR shares via ISS API.

Supports two markets:
  - "forts":  FORTS futures (front-month continuous, e.g. SR, GZ, LK)
  - "shares": TQBR shares (e.g. SBER, GAZP, LKOH)

Uses the public MOEX ISS API (https://iss.moex.com) — no API key needed.

For FORTS futures, automatically resolves the front-month contract by
querying the most liquid active contract for the given base code.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .base import DataProvider

logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")
UTC = ZoneInfo("UTC")

# FORTS contract month codes (1=Jan..12=Dec)
_MONTH_CODE = {1:'F',2:'G',3:'H',4:'J',5:'K',6:'M',7:'N',8:'Q',9:'U',10:'V',11:'X',12:'Z'}
_QUARTERS = [3, 6, 9, 12]

# FORTS base codes → underlying name (for logging)
_FORTS_NAMES = {
    'Si': 'USDRUB', 'Eu': 'EURRUB', 'CR': 'CNYRUB', 'BR': 'BRENT',
    'Ri': 'RTS_INDEX', 'MX': 'IMOEX',
    'SR': 'SBER', 'GZ': 'GAZP', 'LK': 'LKOH', 'MM': 'GMKN', 'RN': 'ROSN',
    'VB': 'VTBR', 'NA': 'NVTK', 'MA': 'MGNT',
    'GD': 'GOLD', 'BT': 'BTC',
}

# Timeframe mapping for ISS candles endpoint
_TF_MAP = {
    "1M": "1", "5M": "5", "10M": "10", "15M": "15",
    "30M": "30", "60M": "60", "1H": "60",
    "4H": "240", "1D": "D", "1W": "W", "1M": "M",
}

# Timeframe mapping for ISS history endpoint (daily only)
_TF_HISTORY = {"1D": "D", "1W": "W"}


class MoexProvider(DataProvider):
    """Live MOEX data provider via ISS API (no auth required).

    Parameters
    ----------
    market : str
        "forts" for FORTS futures, "shares" for TQBR stocks.
    timezone : str
        Display/user timezone (does not affect data, which is always UTC).
    """

    def __init__(self, market: str = "forts", timezone: str = "Europe/Moscow"):
        self.market = market  # "forts" or "shares"
        self.tz = ZoneInfo(timezone)
        self.base_url = "https://iss.moex.com"
        self._connected = False
        # Cache: base_code → current front-month contract secid
        self._front_month_cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # DataProvider interface
    # ------------------------------------------------------------------
    def connect(self) -> bool:
        """ISS is stateless HTTP — just verify reachability."""
        try:
            url = f"{self.base_url}/iss/index.json"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                _ = r.read(100)
            self._connected = True
            logger.info("MoexProvider connected (market=%s)", self.market)
            return True
        except Exception as e:
            logger.error("MoexProvider connect failed: %s", e)
            return False

    def disconnect(self) -> None:
        self._connected = False

    def is_alive(self) -> bool:
        """Check if ISS is reachable."""
        try:
            url = f"{self.base_url}/iss/index.json"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                _ = r.read(50)
            return True
        except Exception:
            return False

    def get_ohlcv(self, symbol: str, timeframe: str, count: int = 200) -> pd.DataFrame:
        """Fetch latest *count* candles for symbol.

        For FORTS futures, *symbol* is the base code (e.g. 'SR', 'GZ').
        The provider resolves the current front-month contract automatically.
        If ISS doesn't support the timeframe natively (e.g. 4H), it fetches
        a finer timeframe and resamples.
        """
        tf_code = _TF_MAP.get(timeframe)
        if tf_code is None:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        # 4H is not natively supported by ISS candles — resample from 60M
        if timeframe == "4H":
            # Fetch 4× count of 1H candles to have enough after resampling
            raw_count = count * 6  # extra for safety
            if self.market == "forts":
                contract = self._resolve_front_month(symbol)
                if contract is None:
                    return pd.DataFrame()
                df = self._fetch_forts_candles(contract, "60", raw_count)
            else:
                df = self._fetch_shares_candles(symbol, "60", raw_count)
            if df.empty:
                return df
            # Resample 1H → 4H
            df = df.resample("4h", label="left", closed="left").agg({
                "open": "first", "high": "max", "low": "min",
                "close": "last", "volume": "sum",
            }).dropna(subset=["open"])
            return df.iloc[-count:]

        if self.market == "forts":
            contract = self._resolve_front_month(symbol)
            if contract is None:
                logger.error("Could not resolve front-month for %s", symbol)
                return pd.DataFrame()
            df = self._fetch_forts_candles(contract, tf_code, count)
        else:
            # TQBR shares
            df = self._fetch_shares_candles(symbol, tf_code, count)

        if df.empty:
            return df
        return df.iloc[-count:]

    def get_ohlcv_range(
        self, symbol: str, timeframe: str, from_dt: datetime, to_dt: datetime
    ) -> pd.DataFrame:
        """Fetch candles for a date range (used by backtest engine)."""
        tf_code = _TF_MAP.get(timeframe)
        if tf_code is None:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        if self.market == "forts":
            # For historical FORTS data, we need to iterate over quarterly contracts
            # and build a continuous front-month series.
            return self._fetch_forts_range(symbol, tf_code, from_dt, to_dt)
        else:
            return self._fetch_shares_range(symbol, tf_code, from_dt, to_dt)

    # ------------------------------------------------------------------
    # FORTS futures helpers
    # ------------------------------------------------------------------
    def _gen_contract_codes(self, base: str, start_year: int, end_year: int) -> list[str]:
        """Generate quarterly FORTS contract codes: SRZ5, SRH6, SRM6..."""
        codes = []
        for y in range(start_year, end_year + 1):
            for m in _QUARTERS:
                yy = y % 10  # 1-digit year
                codes.append(f"{base}{_MONTH_CODE[m]}{yy}")
        return codes

    def _resolve_front_month(self, base: str) -> str | None:
        """Find the most liquid active FORTS contract for a base code.

        Strategy: paginate ISS history (7-day lookback), find highest volume.
        Fallback: guess current quarter contract code.
        """
        if base in self._front_month_cache:
            return self._front_month_cache[base]

        now = datetime.now(MSK)
        lookback = now - timedelta(days=7)

        best_contract = None
        best_volume = -1

        # Paginate through ISS history
        try:
            page = 0
            while page < 10:  # max 10 pages (1000 rows)
                url = (
                    f"{self.base_url}/iss/history/engines/futures/markets/forts/"
                    f"securities.json?from={lookback.strftime('%Y-%m-%d')}"
                    f"&till={now.strftime('%Y-%m-%d')}"
                    f"&start={page}&limit=100&iss.meta=off"
                )
                d = self._iss_get(url)
                h = d.get("history", {})
                cols = h.get("columns", [])
                rows = h.get("data", [])
                if not cols or not rows:
                    break
                i_sec = cols.index("SECID")
                i_vol = cols.index("VOLUME") if "VOLUME" in cols else None

                found_any = False
                for row in rows:
                    if row is None or row[i_sec] is None:
                        continue
                    secid = row[i_sec]
                    if not secid.startswith(base):
                        continue
                    if '-' in secid:
                        continue
                    found_any = True
                    vol = float(row[i_vol]) if i_vol is not None and row[i_vol] else 0
                    if vol > best_volume:
                        best_volume = vol
                        best_contract = secid

                page += 100
                if len(rows) < 100:
                    break
                time.sleep(0.15)
        except Exception as e:
            logger.error("Front-month resolution failed for %s: %s", base, e)

        # Fallback: guess current quarter contract
        if best_contract is None:
            quarter = 3 if now.month >= 7 else (1 if now.month <= 3 else 2)
            # Next quarter month
            q_months = [3, 6, 9, 12]
            next_q = next((m for m in q_months if m >= now.month), q_months[0])
            yy = now.year % 10 if next_q >= now.month else (now.year + 1) % 10
            guess = f"{base}{_MONTH_CODE[next_q]}{yy}"
            logger.info("Front-month guess for %s: %s (no volume data found)", base, guess)
            best_contract = guess

        self._front_month_cache[base] = best_contract
        und = _FORTS_NAMES.get(base, base)
        logger.info("Front-month for %s (%s): %s (vol=%s)", base, und, best_contract, best_volume)
        return best_contract

    def _fetch_forts_candles(self, contract: str, tf_code: str, count: int) -> pd.DataFrame:
        """Fetch intraday/daily candles for a single FORTS contract."""
        # ISS candles endpoint returns max 500 rows
        limit = min(count, 500)
        url = (
            f"{self.base_url}/iss/engines/futures/markets/forts/"
            f"securities/{contract}/candles.json"
            f"?interval={tf_code}&limit={limit}&iss.meta=off"
        )
        d = self._iss_get(url)
        candles = d.get("candles", {})
        cols = candles.get("columns", [])
        rows = candles.get("data", [])
        return self._candles_to_df(cols, rows)

    def _fetch_forts_range(
        self, base: str, tf_code: str, from_dt: datetime, to_dt: datetime
    ) -> pd.DataFrame:
        """Fetch FORTS candles across a date range, stitching quarterly contracts.

        For simplicity, this iterates over all quarterly contracts in the range
        and concatenates data, keeping the most liquid contract per day.
        """
        start_year = from_dt.year
        end_year = to_dt.year
        codes = self._gen_contract_codes(base, start_year, end_year)

        all_dfs = []
        for code in codes:
            try:
                # Fetch daily history for this contract
                url = (
                    f"{self.base_url}/iss/history/engines/futures/markets/forts/"
                    f"securities/{code}.json"
                    f"?from={from_dt.strftime('%Y-%m-%d')}"
                    f"&till={to_dt.strftime('%Y-%m-%d')}&iss.meta=off"
                )
                d = self._iss_get(url)
                h = d.get("history", {})
                cols = h.get("columns", [])
                rows = h.get("data", [])
                if not rows:
                    continue
                df_rows = []
                i_sec = cols.index("SECID")
                i_date = cols.index("TRADEDATE")
                i_open = cols.index("OPEN")
                i_high = cols.index("HIGH")
                i_low = cols.index("LOW")
                i_close = cols.index("CLOSE") if "CLOSE" in cols else cols.index("LEGALCLOSEPRICE")
                i_vol = cols.index("VOLUME") if "VOLUME" in cols else None
                for row in rows:
                    if row is None or row[i_open] is None:
                        continue
                    df_rows.append({
                        "date": row[i_date],
                        "open": float(row[i_open]),
                        "high": float(row[i_high]),
                        "low": float(row[i_low]),
                        "close": float(row[i_close]),
                        "volume": float(row[i_vol]) if i_vol and row[i_vol] else 0.0,
                        "contract": row[i_sec],
                    })
                if df_rows:
                    df = pd.DataFrame(df_rows)
                    df["time"] = pd.to_datetime(df["date"]).dt.tz_localize(MSK).dt.tz_convert(UTC)
                    df = df.set_index("time").sort_index()
                    all_dfs.append(df)
            except Exception as e:
                logger.debug("Contract %s: %s", code, e)
            time.sleep(0.2)

        if not all_dfs:
            return pd.DataFrame()

        big = pd.concat(all_dfs).sort_index()
        # Keep the most liquid contract per date (front-month)
        big["_vol"] = big.groupby(big.index)["volume"].transform("max")
        fm = big[big["volume"] == big["_vol"]].drop(columns=["_vol"])
        fm = fm[~fm.index.duplicated(keep="first")]
        return fm[["open", "high", "low", "close", "volume"]]

    # ------------------------------------------------------------------
    # TQBR shares helpers
    # ------------------------------------------------------------------
    def _fetch_shares_candles(self, symbol: str, tf_code: str, count: int) -> pd.DataFrame:
        """Fetch intraday candles for a TQBR share."""
        limit = min(count, 500)
        url = (
            f"{self.base_url}/iss/engines/stock/markets/shares/"
            f"securities/{symbol}/candles.json"
            f"?interval={tf_code}&limit={limit}&iss.meta=off"
        )
        d = self._iss_get(url)
        candles = d.get("candles", {})
        cols = candles.get("columns", [])
        rows = candles.get("data", [])
        return self._candles_to_df(cols, rows)

    def _fetch_shares_range(
        self, symbol: str, tf_code: str, from_dt: datetime, to_dt: datetime
    ) -> pd.DataFrame:
        """Fetch daily history for a TQBR share over a date range."""
        all_rows = []
        cursor = from_dt
        while cursor <= to_dt:
            win_end = min(cursor + timedelta(days=200), to_dt)
            url = (
                f"{self.base_url}/iss/history/engines/stock/markets/shares/"
                f"boards/TQBR/securities/{symbol}.json"
                f"?from={cursor.strftime('%Y-%m-%d')}&till={win_end.strftime('%Y-%m-%d')}"
                f"&iss.meta=off"
            )
            d = self._iss_get(url)
            h = d.get("history", {})
            cols = h.get("columns", [])
            rows = h.get("data", [])
            if cols and rows:
                i_open = cols.index("OPEN")
                i_high = cols.index("HIGH")
                i_low = cols.index("LOW")
                i_close = cols.index("LEGALCLOSEPRICE") if "LEGALCLOSEPRICE" in cols else cols.index("CLOSE")
                i_vol = cols.index("VOLUME") if "VOLUME" in cols else None
                i_date = cols.index("TRADEDATE")
                for row in rows:
                    if row is None or row[i_open] is None:
                        continue
                    all_rows.append({
                        "date": row[i_date],
                        "open": float(row[i_open]),
                        "high": float(row[i_high]),
                        "low": float(row[i_low]),
                        "close": float(row[i_close]),
                        "volume": float(row[i_vol]) if i_vol and row[i_vol] else 0.0,
                    })
            cursor = win_end + timedelta(days=1)
            time.sleep(0.2)

        if not all_rows:
            return pd.DataFrame()
        df = pd.DataFrame(all_rows)
        df["time"] = pd.to_datetime(df["date"]).dt.tz_localize(MSK).dt.tz_convert(UTC)
        df = df.set_index("time").sort_index()
        df = df[~df.index.duplicated(keep="last")]
        return df[["open", "high", "low", "close", "volume"]]

    # ------------------------------------------------------------------
    # Common helpers
    # ------------------------------------------------------------------
    def _iss_get(self, url: str, retries: int = 3) -> dict:
        """GET JSON from ISS with retry."""
        last_err = None
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception as e:
                last_err = e
                time.sleep(1.0 * (attempt + 1))
        raise RuntimeError(f"ISS GET failed: {last_err}\nURL: {url}")

    @staticmethod
    def _candles_to_df(cols: list, rows: list) -> pd.DataFrame:
        """Convert ISS candles response to standard DataFrame."""
        if not cols or not rows:
            return pd.DataFrame()
        # ISS candle columns: [begin, open, high, low, close, volume, ...]
        i_begin = cols.index("begin") if "begin" in cols else 0
        i_open = cols.index("open") if "open" in cols else 1
        i_high = cols.index("high") if "high" in cols else 2
        i_low = cols.index("low") if "low" in cols else 3
        i_close = cols.index("close") if "close" in cols else 4
        i_vol = cols.index("volume") if "volume" in cols else 5

        df_rows = []
        for row in rows:
            begin = row[i_begin]
            if begin is None:
                continue
            df_rows.append({
                "begin": begin,
                "open": float(row[i_open]),
                "high": float(row[i_high]),
                "low": float(row[i_low]),
                "close": float(row[i_close]),
                "volume": float(row[i_vol]) if row[i_vol] else 0.0,
            })
        if not df_rows:
            return pd.DataFrame()
        df = pd.DataFrame(df_rows)
        # Parse MSK timestamp and convert to UTC
        df["time"] = pd.to_datetime(df["begin"]).dt.tz_localize(MSK).dt.tz_convert(UTC)
        df = df.set_index("time").sort_index()
        df = df[~df.index.duplicated(keep="last")]
        return df[["open", "high", "low", "close", "volume"]]
