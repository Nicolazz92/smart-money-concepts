"""Resolve win/loss outcome for each backtest signal."""

from __future__ import annotations

import pandas as pd

from .models import BacktestSignal


def resolve_outcomes(
    signals: list[BacktestSignal],
    ohlcv_ob: pd.DataFrame,
    cost_model=None,
) -> list[BacktestSignal]:
    """Walk forward from each signal to determine if TP1 or SL was hit first.

    Rules:
      - BUY:  candle.low  <= SL → LOSS, candle.high >= TP1 → WIN
      - SELL: candle.high >= SL → LOSS, candle.low  <= TP1 → WIN
      - Same candle hits both → LOSS (conservative, no tick data)
      - End of data without either → EXPIRED

    If *cost_model* (a CostModel instance) is supplied, round-trip trading
    costs are deducted from winners and added to losers, expressed in
    R-multiples via the trade's own entry→SL distance.
    """
    highs = ohlcv_ob["high"].values
    lows = ohlcv_ob["low"].values
    index = ohlcv_ob.index

    for sig in signals:
        if sig.tp1 is None:
            sig.outcome = "NO_TP"
            continue

        # Find first candle AFTER the signal candle
        start = index.searchsorted(sig.signal_time, side="right")
        if start >= len(index):
            sig.outcome = "EXPIRED"
            continue

        # Pre-compute cost as R-debit if a cost model is supplied.
        # risk_per_share = |entry - sl|; round-trip cost expressed per share.
        cost_in_r = 0.0
        if cost_model is not None:
            risk = abs(sig.entry - sig.sl)
            if risk > 0:
                # cost_as_price_delta returns per-share round-trip cost
                cost_in_r = cost_model.cost_as_price_delta(sig.entry) / risk

        resolved = False
        for j in range(start, len(index)):
            h = highs[j]
            l = lows[j]

            if sig.direction == 1:  # BUY
                sl_hit = l <= sig.sl
                tp_hit = h >= sig.tp1
            else:  # SELL
                sl_hit = h >= sig.sl
                tp_hit = l <= sig.tp1

            if sl_hit and tp_hit:
                # Both hit on same candle — conservative = LOSS
                sig.outcome = "LOSS"
                sig.actual_rr = -1.0 - cost_in_r
                sig.exit_time = index[j]
                sig.exit_price = sig.sl
                resolved = True
                break
            elif tp_hit:
                sig.outcome = "WIN_TP1"
                sig.actual_rr = (sig.rr1 or 0.0) - cost_in_r
                sig.exit_time = index[j]
                sig.exit_price = sig.tp1
                resolved = True
                break
            elif sl_hit:
                sig.outcome = "LOSS"
                sig.actual_rr = -1.0 - cost_in_r
                sig.exit_time = index[j]
                sig.exit_price = sig.sl
                resolved = True
                break

        if not resolved:
            sig.outcome = "EXPIRED"

    return signals
