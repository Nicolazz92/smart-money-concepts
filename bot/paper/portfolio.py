"""Portfolio tracker for paper trading.

Tracks virtual equity, open positions, and realized P/L.
Position sizing: configurable % of equity per trade (default: full deposit).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class PaperPosition:
    """A single open paper position."""

    id: Optional[int] = None
    ticker: str = ""
    direction: str = "LONG"  # "LONG" or "SHORT"
    entry: float = 0.0
    sl: float = 0.0
    tp1: float = 0.0
    rr1: float = 0.0
    signal_time: str = ""
    open_time: str = ""
    position_size_pct: float = 100.0
    # Filled on close:
    status: str = "OPEN"  # OPEN, WIN_TP1, LOSS
    exit_time: str = ""
    exit_price: float = 0.0
    pnl_pct: float = 0.0  # % of equity gained/lost

    @property
    def is_long(self) -> bool:
        return self.direction.upper() in ("LONG", "BUY", "1", 1)

    @property
    def risk_pct(self) -> float:
        """Risk as % of entry price (distance entry→SL)."""
        if self.entry <= 0:
            return 0.0
        return abs(self.entry - self.sl) / self.entry * 100


class Portfolio:
    """Tracks equity and positions for paper trading.

    Parameters
    ----------
    initial_capital : float
        Starting equity (e.g. 100000 RUB).
    position_pct_long : float
        % of free equity allocated per LONG position (default 100).
    position_pct_short : float
        % of free equity allocated per SHORT position (default 50).
    max_concurrent : int
        Maximum simultaneously open positions (default 3).
    cost_pct : float
        Round-trip cost as % of position value (e.g. 0.26 for Finam Strateg).
    """

    def __init__(
        self,
        initial_capital: float = 100000.0,
        position_pct_long: float = 100.0,
        position_pct_short: float = 50.0,
        max_concurrent: int = 3,
        cost_pct: float = 0.26,
    ):
        self.initial_capital = initial_capital
        self.equity = initial_capital
        self.position_pct_long = position_pct_long
        self.position_pct_short = position_pct_short
        self.max_concurrent = max_concurrent
        self.cost_pct = cost_pct  # round-trip cost %
        self.open_positions: list[PaperPosition] = []
        self.closed_positions: list[PaperPosition] = []
        self.realized_pnl = 0.0

    @property
    def free_equity(self) -> float:
        """Equity not allocated to open positions."""
        allocated = sum(
            self.equity * (p.position_size_pct / 100) for p in self.open_positions
        )
        return max(self.equity - allocated, 0.0)

    @property
    def can_open(self) -> bool:
        return len(self.open_positions) < self.max_concurrent

    def open_position(
        self,
        ticker: str,
        direction: str,
        entry: float,
        sl: float,
        tp1: float,
        rr1: float = 0.0,
        signal_time: str = "",
    ) -> PaperPosition | None:
        """Open a new paper position. Returns None if can't (no free equity)."""
        if not self.can_open:
            logger.info("Cannot open %s: max_concurrent=%d reached", ticker, self.max_concurrent)
            return None

        is_long = direction.upper() in ("LONG", "BUY", "1", 1)
        size_pct = self.position_pct_long if is_long else self.position_pct_short

        # Check if enough free equity
        if self.free_equity < self.equity * (size_pct / 100) * 0.01:
            logger.info("Cannot open %s: not enough free equity", ticker)
            return None

        pos = PaperPosition(
            ticker=ticker,
            direction="LONG" if is_long else "SHORT",
            entry=entry,
            sl=sl,
            tp1=tp1,
            rr1=rr1,
            signal_time=signal_time,
            open_time=datetime.now(timezone.utc).isoformat(),
            position_size_pct=size_pct,
        )
        self.open_positions.append(pos)
        logger.info(
            "PAPER OPEN %s %s @ %s  SL=%s  TP1=%s  RR=%s  size=%.0f%%",
            pos.direction, ticker, entry, sl, tp1, rr1, size_pct,
        )
        return pos

    def close_position(self, pos: PaperPosition, exit_price: float, outcome: str, exit_time: str):
        """Close a position and realize P/L."""
        is_long = pos.is_long

        # Price move fraction
        if is_long:
            if outcome == "WIN_TP1":
                move_pct = (exit_price - pos.entry) / pos.entry
            else:  # LOSS
                move_pct = (pos.exit_price or exit_price) - pos.entry
                move_pct = (exit_price - pos.entry) / pos.entry  # negative
        else:
            if outcome == "WIN_TP1":
                move_pct = (pos.entry - exit_price) / pos.entry
            else:  # LOSS
                move_pct = (pos.entry - exit_price) / pos.entry  # negative

        # P/L = position_size% of equity × price_move − cost
        cost_fraction = self.cost_pct / 100
        pnl_pct = (pos.position_size_pct / 100) * move_pct - cost_fraction
        pnl_absolute = self.equity * pnl_pct

        self.equity *= (1 + pnl_pct)
        pos.status = outcome
        pos.exit_time = exit_time
        pos.exit_price = exit_price
        pos.pnl_pct = pnl_pct * 100

        self.realized_pnl += pnl_absolute
        self.open_positions.remove(pos)
        self.closed_positions.append(pos)

        logger.info(
            "PAPER CLOSE %s %s  outcome=%s  exit=%s  P/L=%.2f%%  equity=%.0f",
            pos.ticker, pos.direction, outcome, exit_price, pos.pnl_pct, self.equity,
        )
        return pos

    def reset(self):
        """Reset to initial state."""
        self.equity = self.initial_capital
        self.open_positions.clear()
        self.closed_positions.clear()
        self.realized_pnl = 0.0
        logger.info("Portfolio reset to %.0f", self.equity)

    def stats(self) -> dict:
        """Return summary statistics."""
        total = len(self.closed_positions)
        wins = sum(1 for p in self.closed_positions if p.status == "WIN_TP1")
        losses = sum(1 for p in self.closed_positions if p.status == "LOSS")
        wr = wins / total * 100 if total > 0 else 0
        return {
            "equity": round(self.equity, 2),
            "initial_capital": self.initial_capital,
            "return_pct": round((self.equity / self.initial_capital - 1) * 100, 2),
            "open_positions": len(self.open_positions),
            "total_closed": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wr, 1),
        }
