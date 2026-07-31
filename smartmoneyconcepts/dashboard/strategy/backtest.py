from datetime import datetime

import numpy as np

from ..db.base import BaseCandleRepository, Candle
from ..engine.candle_aggregator import aggregate_candles, TIMEFRAME_MINUTES
from ..engine.indicators import IndicatorService
from .evaluator import StrategyEvaluator
from .models import BacktestResult, Strategy, Trade


class Backtester:
    def __init__(self, repo: BaseCandleRepository, indicator_service: IndicatorService, cost_pct: float = 0.0):
        self.repo = repo
        self.indicators = indicator_service
        self.cost_pct = cost_pct
        self.evaluator = StrategyEvaluator(cost_pct=cost_pct)

    def run(
        self,
        strategy: Strategy,
        start: datetime,
        end: datetime,
        initial_capital: float = 10000.0,
    ) -> BacktestResult:
        candles = self._fetch_candles(strategy, start, end)
        if len(candles) < 20:
            return BacktestResult(
                strategy=strategy.name,
                symbol=strategy.symbol,
                timeframe=strategy.timeframe,
                total_trades=0,
                wins=0,
                losses=0,
                win_rate=0.0,
                total_pnl=0.0,
                profit_factor=0.0,
                max_drawdown=0.0,
                sharpe=0.0,
                avg_win=0.0,
                avg_loss=0.0,
                largest_win=0.0,
                largest_loss=0.0,
                avg_bars_held=0.0,
                trades=[],
            )

        indicators = self.indicators.calculate(
            strategy.symbol, strategy.timeframe, len(candles)
        )
        if "error" in indicators:
            return BacktestResult(
                strategy=strategy.name,
                symbol=strategy.symbol,
                timeframe=strategy.timeframe,
                total_trades=0,
                wins=0,
                losses=0,
                win_rate=0.0,
                total_pnl=0.0,
                profit_factor=0.0,
                max_drawdown=0.0,
                sharpe=0.0,
                avg_win=0.0,
                avg_loss=0.0,
                largest_win=0.0,
                largest_loss=0.0,
                avg_bars_held=0.0,
                trades=[],
            )

        trades = self.evaluator.run(strategy, candles, indicators)
        return self._compute_results(strategy, trades, initial_capital)

    def _fetch_candles(
        self, strategy: Strategy, start: datetime, end: datetime
    ) -> list[Candle]:
        candles = self.repo.get_candles(
            strategy.symbol,
            strategy.timeframe,
            limit=50000,
            since=start,
        )
        candles = [c for c in candles if c.timestamp <= end]
        if len(candles) < 20 and strategy.timeframe != "1m":
            tf_minutes = TIMEFRAME_MINUTES.get(strategy.timeframe, 60)
            one_min_candles = self.repo.get_candles(
                strategy.symbol, "1m", limit=50000, since=start
            )
            one_min_candles = [c for c in one_min_candles if c.timestamp <= end]
            if len(one_min_candles) >= 10:
                candles = aggregate_candles(one_min_candles, strategy.timeframe)
        return candles

    def _compute_results(
        self,
        strategy: Strategy,
        trades: list[Trade],
        initial_capital: float,
    ) -> BacktestResult:
        closed = [t for t in trades if t.status == "closed" and t.pnl is not None]
        if not closed:
            return BacktestResult(
                strategy=strategy.name,
                symbol=strategy.symbol,
                timeframe=strategy.timeframe,
                total_trades=0,
                wins=0,
                losses=0,
                win_rate=0.0,
                total_pnl=0.0,
                profit_factor=0.0,
                max_drawdown=0.0,
                sharpe=0.0,
                avg_win=0.0,
                avg_loss=0.0,
                largest_win=0.0,
                largest_loss=0.0,
                avg_bars_held=0.0,
                trades=[],
            )

        wins = [t for t in closed if t.pnl > 0]
        losses = [t for t in closed if t.pnl <= 0]
        total_pnl = sum(t.pnl for t in closed)
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        win_rate = len(wins) / len(closed)

        profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")

        avg_win = (gross_win / len(wins)) if wins else 0.0
        avg_loss = (gross_loss / len(losses)) if losses else 0.0
        largest_win = max(t.pnl for t in wins) if wins else 0.0
        largest_loss = min(t.pnl for t in losses) if losses else 0.0

        bars_held = [
            (t.exit_index or 0) - t.entry_index
            for t in closed
            if t.exit_index is not None
        ]
        avg_bars_held = sum(bars_held) / len(bars_held) if bars_held else 0.0

        equity_curve = self._build_equity_curve(closed, initial_capital)
        max_drawdown = self._calc_max_drawdown(equity_curve)
        sharpe = self._calc_sharpe(equity_curve)

        return BacktestResult(
            strategy=strategy.name,
            symbol=strategy.symbol,
            timeframe=strategy.timeframe,
            total_trades=len(closed),
            wins=len(wins),
            losses=len(losses),
            win_rate=round(win_rate, 4),
            total_pnl=round(total_pnl, 2),
            profit_factor=round(profit_factor, 4)
            if profit_factor != float("inf")
            else float("inf"),
            max_drawdown=round(max_drawdown, 4),
            sharpe=round(sharpe, 4),
            avg_win=round(avg_win, 2),
            avg_loss=round(avg_loss, 2),
            largest_win=round(largest_win, 2),
            largest_loss=round(largest_loss, 2),
            avg_bars_held=round(avg_bars_held, 2),
            trades=closed,
        )

    def _build_equity_curve(
        self, trades: list[Trade], initial_capital: float
    ) -> list[float]:
        curve = [initial_capital]
        equity = initial_capital
        for t in trades:
            equity += t.pnl or 0
            curve.append(equity)
        return curve

    def _calc_max_drawdown(self, equity: list[float]) -> float:
        if len(equity) < 2:
            return 0.0
        peak = equity[0]
        max_dd = 0.0
        for v in equity:
            if v > peak:
                peak = v
            dd = (peak - v) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
        return max_dd

    def _calc_sharpe(self, equity: list[float]) -> float:
        if len(equity) < 3:
            return 0.0
        returns = [
            (equity[i] - equity[i - 1]) / equity[i - 1]
            for i in range(1, len(equity))
            if equity[i - 1] > 0
        ]
        if len(returns) < 2:
            return 0.0
        avg_ret = np.mean(returns)
        std_ret = np.std(returns, ddof=1)
        if std_ret == 0:
            return 0.0
        return (avg_ret / std_ret) * np.sqrt(365)
