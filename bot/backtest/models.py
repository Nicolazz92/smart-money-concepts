"""Data models for backtest results."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class BacktestSignal:
    """A single signal generated during the backtest."""

    pair: str
    direction: int  # 1 (buy) or -1 (sell)
    bias_type: str  # "BOS" or "CHOCH"
    signal_time: datetime
    ob_time: datetime | None  # when the OB candle closed
    entry: float
    sl: float
    tp1: float | None
    tp2: float | None
    rr1: float | None
    rr2: float | None
    ob_strength: float
    ob_size_pips: float
    sl_pips: float
    tp1_pips: float | None
    tp2_pips: float | None
    # Filled by outcome resolver
    outcome: str = "PENDING"  # WIN_TP1, LOSS, EXPIRED, NO_TP
    actual_rr: float | None = None
    exit_time: datetime | None = None
    exit_price: float | None = None


@dataclass
class BacktestResult:
    """Aggregate results of a backtest run."""

    signals: list[BacktestSignal] = field(default_factory=list)
    start_date: str = ""
    end_date: str = ""
    pairs: list[str] = field(default_factory=list)
    # Stats (computed after outcome resolution)
    total_signals: int = 0
    wins: int = 0
    losses: int = 0
    no_tp: int = 0
    expired: int = 0
    win_rate: float = 0.0
    avg_rr_winners: float = 0.0
    profit_factor: float = 0.0
    total_rr: float = 0.0
    pair_stats: dict = field(default_factory=dict)
    # Diagnostic counters — help identify where signals are lost in the pipeline
    diag_total_obs_found: int = 0        # OBs detected (all directions)
    diag_obs_in_confluence: int = 0      # OBs matching the bias direction
    diag_price_returns: int = 0          # price entered an OB zone
    diag_filtered_by_kz: int = 0         # rejected by kill zone filter
    diag_filtered_by_rr: int = 0         # rejected by min R:R filter

    def compute_stats(self) -> None:
        self.total_signals = len(self.signals)
        self.wins = sum(1 for s in self.signals if s.outcome == "WIN_TP1")
        self.losses = sum(1 for s in self.signals if s.outcome == "LOSS")
        self.no_tp = sum(1 for s in self.signals if s.outcome == "NO_TP")
        self.expired = sum(1 for s in self.signals if s.outcome == "EXPIRED")

        decided = self.wins + self.losses
        self.win_rate = (self.wins / decided * 100) if decided > 0 else 0.0

        winner_rrs = [s.actual_rr for s in self.signals if s.outcome == "WIN_TP1" and s.actual_rr]
        loser_rrs = [abs(s.actual_rr) for s in self.signals if s.outcome == "LOSS" and s.actual_rr]
        self.avg_rr_winners = sum(winner_rrs) / len(winner_rrs) if winner_rrs else 0.0
        self.total_rr = sum(s.actual_rr for s in self.signals if s.actual_rr is not None)

        gross_profit = sum(winner_rrs)
        gross_loss = sum(loser_rrs)
        self.profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0

        # Per-pair breakdown
        pairs_seen: dict[str, list[BacktestSignal]] = {}
        for s in self.signals:
            pairs_seen.setdefault(s.pair, []).append(s)

        self.pair_stats = {}
        for pair, sigs in pairs_seen.items():
            w = sum(1 for s in sigs if s.outcome == "WIN_TP1")
            l = sum(1 for s in sigs if s.outcome == "LOSS")
            d = w + l
            self.pair_stats[pair] = {
                "signals": len(sigs),
                "wins": w,
                "losses": l,
                "win_rate": (w / d * 100) if d > 0 else 0.0,
                "total_rr": sum(s.actual_rr for s in sigs if s.actual_rr is not None),
            }
