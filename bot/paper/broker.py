"""Paper broker: checks open positions against live candles and resolves TP/SL.

Reuses the same conservative TP/SL logic as backtest/outcome.py:
  - Same candle hits both TP and SL → LOSS (conservative)
  - Walk forward candle-by-candle from open_time
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd

from .portfolio import Portfolio, PaperPosition

logger = logging.getLogger(__name__)


class PaperBroker:
    """Simulated broker that resolves paper positions against live OHLCV.

    Parameters
    ----------
    portfolio : Portfolio
        The portfolio to operate on.
    """

    def __init__(self, portfolio: Portfolio):
        self.portfolio = portfolio

    def check_and_close(self, candles_by_ticker: dict[str, pd.DataFrame]) -> list[PaperPosition]:
        """Check all open positions against latest candles.

        Parameters
        ----------
        candles_by_ticker : dict
            {ticker: DataFrame} with OHLCV data (high, low columns required).
            Only candles AFTER the position's open_time are checked.

        Returns
        -------
        list of PaperPosition
            Positions that were closed in this check.
        """
        closed = []
        positions_to_check = list(self.portfolio.open_positions)

        for pos in positions_to_check:
            df = candles_by_ticker.get(pos.ticker)
            if df is None or df.empty:
                continue

            # Find candles after position open time
            open_dt = pd.Timestamp(pos.open_time)
            if open_dt.tzinfo is None:
                open_dt = open_dt.tz_localize("UTC")
            future = df[df.index > open_dt]
            if future.empty:
                continue

            # Walk forward to find first TP or SL hit
            for ts, row in future.iterrows():
                high = float(row["high"])
                low = float(row["low"])

                if pos.is_long:
                    sl_hit = low <= pos.sl
                    tp_hit = high >= pos.tp1
                else:
                    sl_hit = high >= pos.sl
                    tp_hit = low <= pos.tp1

                if sl_hit and tp_hit:
                    # Both hit on same candle — conservative = LOSS
                    self.portfolio.close_position(
                        pos, pos.sl, "LOSS", ts.isoformat()
                    )
                    closed.append(pos)
                    break
                elif tp_hit:
                    self.portfolio.close_position(
                        pos, pos.tp1, "WIN_TP1", ts.isoformat()
                    )
                    closed.append(pos)
                    break
                elif sl_hit:
                    self.portfolio.close_position(
                        pos, pos.sl, "LOSS", ts.isoformat()
                    )
                    closed.append(pos)
                    break

        return closed

    def open_from_signal(self, trade: dict, signal_time: str = "") -> PaperPosition | None:
        """Open a paper position from a trade signal dict.

        Expected keys: ticker, direction (BUY/SELL or 1/-1), entry, sl, tp1, rr1.
        """
        ticker = trade.get("symbol", trade.get("ticker", ""))
        direction_raw = trade.get("direction", 1)
        direction = "LONG" if direction_raw in (1, "BUY", "1", "LONG") else "SHORT"
        entry = float(trade.get("entry", 0))
        sl = float(trade.get("sl", 0))
        tp1 = float(trade.get("tp1", 0))
        rr1 = float(trade.get("rr1", 0))

        if not ticker or entry <= 0 or sl <= 0:
            logger.warning("Invalid signal for paper trade: %s", trade)
            return None

        return self.portfolio.open_position(
            ticker=ticker,
            direction=direction,
            entry=entry,
            sl=sl,
            tp1=tp1,
            rr1=rr1,
            signal_time=signal_time,
        )
