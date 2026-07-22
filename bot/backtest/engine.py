"""Backtest engine — replays the strategy on historical data using a sliding window."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

import numpy as np
import pandas as pd
from smartmoneyconcepts import smc

from bot.config import StrategyConfig
from bot.data_providers.base import DataProvider
from bot.notifier.telegram import TelegramNotifier
from bot.strategy.bias import get_bias
from bot.strategy.killzone import is_in_killzone
from bot.strategy.trade_calculator import calculate_trade

from .models import BacktestResult, BacktestSignal
from .outcome import resolve_outcomes

logger = logging.getLogger(__name__)

# Approximate candle durations used to compute lookback
_TF_HOURS = {
    "5M": 5 / 60,
    "15M": 15 / 60,
    "30M": 0.5,
    "1H": 1,
    "4H": 4,
    "1D": 24,
    "1W": 168,
}

# Same window size as the live scanner
_OB_WINDOW = 500
_BIAS_WINDOW = 200


class BacktestEngine:
    """Run the SMC strategy over a historical date range.

    Realism knobs (default = original SNahary behaviour, idealised):
      min_rr          : drop signals whose rr1 (entry→TP1 / entry→SL) is below
                        this threshold. Filters out " TP1 right next to entry"
                        setups that mechanically inflate win rate.
      cost_per_trade  : currency units subtracted from every closed trade
                        (broker commission + half-spread + estimated slippage).
                        Applied symmetrically: wins shrink, losses grow.
    """

    def __init__(
        self,
        provider: DataProvider,
        strategy: StrategyConfig,
        notifier: TelegramNotifier | None = None,
        min_rr: float = 0.0,
        cost_per_trade: float = 0.0,
        cost_model=None,
    ):
        self.provider = provider
        self.strategy = strategy
        self.notifier = notifier
        self.min_rr = min_rr
        self.cost_per_trade = cost_per_trade
        # CostModel instance (overrides cost_per_trade if set). When None
        # and cost_per_trade > 0, a simple per-trade RUB cost is applied.
        self.cost_model = cost_model

    # ------------------------------------------------------------------
    def run(
        self,
        start_date: str,
        end_date: str,
        pairs: list[str] | None = None,
        progress_cb: Callable[[str, int, int], None] | None = None,
    ) -> BacktestResult:
        pairs = pairs or self.strategy.pairs
        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc
        )

        # Lookback: enough history before start for the first bias + OB window
        bias_hours = _TF_HOURS.get(self.strategy.bias_timeframe, 4)
        ob_hours = _TF_HOURS.get(self.strategy.ob_timeframe, 0.5)
        lookback = timedelta(
            hours=max(bias_hours * (_BIAS_WINDOW + 20), ob_hours * (_OB_WINDOW + 20))
        )

        all_signals: list[BacktestSignal] = []
        total_diag = [0, 0, 0, 0, 0]  # obs_found, confluence, returns, filtered_kz, filtered_rr

        for pair in pairs:
            try:
                signals, *diag = self._backtest_pair(
                    pair, start_dt, end_dt, lookback, progress_cb
                )
                all_signals.extend(signals)
                for j in range(5):
                    total_diag[j] += diag[j]
            except Exception as exc:
                logger.exception("Backtest error for %s", pair)
                if progress_cb:
                    progress_cb(f"{pair}: ERREUR — {exc}", 0, 0)

        result = BacktestResult(
            signals=all_signals,
            start_date=start_date,
            end_date=end_date,
            pairs=pairs,
        )
        result.diag_total_obs_found = total_diag[0]
        result.diag_obs_in_confluence = total_diag[1]
        result.diag_price_returns = total_diag[2]
        result.diag_filtered_by_kz = total_diag[3]
        result.diag_filtered_by_rr = total_diag[4]
        result.compute_stats()
        return result

    # ------------------------------------------------------------------
    def _backtest_pair(
        self,
        pair: str,
        start_dt: datetime,
        end_dt: datetime,
        lookback: timedelta,
        progress_cb: Callable | None,
    ) -> list[BacktestSignal]:
        logger.info("Backtest %s  [%s → %s]", pair, start_dt.date(), end_dt.date())

        # 1. Fetch full dataset (with lookback for initial window)
        fetch_from = start_dt - lookback
        ohlcv_ob_full = self.provider.get_ohlcv_range(
            pair, self.strategy.ob_timeframe, fetch_from, end_dt
        )
        ohlcv_bias_full = self.provider.get_ohlcv_range(
            pair, self.strategy.bias_timeframe, fetch_from, end_dt
        )

        logger.info(
            "%s — fetched %d OB candles, %d bias candles",
            pair, len(ohlcv_ob_full), len(ohlcv_bias_full),
        )

        # 2. Determine bias recalculation timestamps (when new HTF candle forms)
        bias_times = ohlcv_bias_full.index
        bias_in_range = bias_times[bias_times >= start_dt]

        # 3. Iterate candle-by-candle on the OB TF within [start, end]
        ob_in_range = ohlcv_ob_full[ohlcv_ob_full.index >= start_dt]
        total_candles = len(ob_in_range)
        current_bias: dict | None = None
        next_bias_idx = 0
        dedup: set = set()
        signals: list[BacktestSignal] = []

        # Current active OBs (recalculated at each bias change)
        active_obs_list: list[dict] = []
        current_swings: pd.DataFrame | None = None
        current_ob_slice: pd.DataFrame | None = None
        current_bias_slice: pd.DataFrame | None = None

        # Diagnostic counters
        diag_obs_found = 0
        diag_obs_confluence = 0
        diag_price_returns = 0
        diag_filtered_kz = 0
        diag_filtered_rr = 0

        for i, (ts, candle) in enumerate(ob_in_range.iterrows()):
            # Progress
            if progress_cb and i % 100 == 0:
                progress_cb(pair, i, total_candles)

            # ── Recalculate bias + OBs when a new HTF candle forms ──
            recalc = False
            while next_bias_idx < len(bias_in_range) and bias_in_range[next_bias_idx] <= ts:
                bias_ts = bias_in_range[next_bias_idx]
                next_bias_idx += 1
                recalc = True

                # Bias: sliding window of last 200 candles
                current_bias_slice = ohlcv_bias_full[:bias_ts].iloc[-_BIAS_WINDOW:]
                current_bias = get_bias(
                    current_bias_slice,
                    swing_length=self.strategy.bias_swing_length,
                )

            if recalc and current_bias and current_bias["direction"] != 0:
                # OBs: sliding window of 500 candles + lookahead buffer
                # The lookahead gives swing_highs_lows enough future candles
                # to confirm swings at the window edge (avoids NaN dead zone).
                lookahead = self.strategy.ob_swing_length
                ts_pos = ohlcv_ob_full.index.get_loc(ts)
                if isinstance(ts_pos, slice):
                    ts_pos = ts_pos.stop
                end_pos = min(len(ohlcv_ob_full), ts_pos + lookahead + 1)
                start_pos = max(0, end_pos - _OB_WINDOW - lookahead)
                current_ob_slice = ohlcv_ob_full.iloc[start_pos:end_pos]
                current_swings = smc.swing_highs_lows(
                    current_ob_slice,
                    swing_length=self.strategy.ob_swing_length,
                )
                obs_window = smc.ob(current_ob_slice, current_swings)

                # Count ALL OBs found (diagnostic)
                all_obs = obs_window["OB"].dropna()
                diag_obs_found += len(all_obs)

                # Filter: direction matches bias + unmitigated
                direction = current_bias["direction"]
                mask = (obs_window["OB"] == direction) & (
                    obs_window["MitigatedIndex"].isna()
                    | (obs_window["MitigatedIndex"] == 0)
                )
                active_df = obs_window[mask]
                diag_obs_confluence += len(active_df)

                active_obs_list = []
                for ob_idx, ob_row in active_df.iterrows():
                    ob_time = current_ob_slice.index[int(ob_idx)] if int(ob_idx) < len(current_ob_slice) else None
                    active_obs_list.append({
                        "ob_index": int(ob_idx),
                        "top": float(ob_row["Top"]),
                        "bottom": float(ob_row["Bottom"]),
                        "percentage": float(ob_row["Percentage"]),
                        "volume": float(ob_row["OBVolume"]),
                        "ob_time": ob_time,
                        "alive": True,
                    })

                if recalc:
                    bias_label = "BULL" if direction == 1 else "BEAR"
                    logger.debug(
                        "%s — Bias: %s (%s), %d OBs total, %d en confluence, %d actifs",
                        pair,
                        bias_label,
                        current_bias["type"],
                        len(all_obs),
                        len(active_df),
                        len(active_obs_list),
                    )

            # ── Skip if no bias or no active OBs ──
            if current_bias is None or current_bias["direction"] == 0:
                continue
            if not active_obs_list:
                continue

            direction = current_bias["direction"]

            # ── Track OB mitigation between recalculations ──
            # (always tracked regardless of killzone — OBs die outside KZ too)
            for ob in active_obs_list:
                if not ob["alive"]:
                    continue
                if direction == 1 and candle["low"] < ob["bottom"]:
                    ob["alive"] = False
                elif direction == -1 and candle["high"] > ob["top"]:
                    ob["alive"] = False

            # ── Kill zone filter ──
            if not is_in_killzone(ts, self.strategy.killzones):
                diag_filtered_kz += 1
                continue

            # ── Check if current candle enters any active OB zone ──
            for ob in active_obs_list:
                if not ob["alive"]:
                    continue

                if direction == 1:
                    in_zone = candle["low"] <= ob["top"] and candle["close"] >= ob["bottom"]
                else:
                    in_zone = candle["high"] >= ob["bottom"] and candle["close"] <= ob["top"]

                if not in_zone:
                    continue

                diag_price_returns += 1

                dedup_key = (pair, ob["ob_index"], ob["top"], ob["bottom"])
                if dedup_key in dedup:
                    continue
                dedup.add(dedup_key)

                # ── Calculate trade info ──
                trade = calculate_trade(
                    symbol=pair,
                    bias_direction=direction,
                    ob=ob,
                    swings=current_swings,
                    ohlcv_ob_tf=current_ob_slice,
                    ohlcv_bias_tf=current_bias_slice,
                    sl_buffer_pips=self.strategy.sl_buffer_pips,
                )

                # ── R:R realism filter ──
                # Drop setups where TP1 is too close to entry (rr1 < min_rr).
                # This kills the "any touch wins 0.1R" trades that inflate
                # win rate mechanically. OB is left alive so a better setup
                # on a later candle can still trigger.
                rr1 = trade.get("rr1")
                if rr1 is None or rr1 < self.min_rr:
                    diag_filtered_rr += 1
                    continue

                # All signals are recorded. Telegram alert always sent
                # (bot indicateur — the trader filters via the checklist).
                trade["signal_time"] = ts  # candle timestamp that touched the OB
                trade["ob_time"] = ob.get("ob_time")  # OB formation time

                if self.notifier:
                    self.notifier.send_signal(trade, current_bias)

                sig = BacktestSignal(
                    pair=pair,
                    direction=direction,
                    bias_type=current_bias["type"],
                    signal_time=ts,
                    ob_time=ob.get("ob_time"),
                    entry=trade["entry"],
                    sl=trade["sl"],
                    tp1=trade.get("tp1"),
                    tp2=trade.get("tp2"),
                    rr1=trade.get("rr1"),
                    rr2=trade.get("rr2"),
                    ob_strength=trade.get("ob_strength", 0),
                    ob_size_pips=trade.get("ob_size_pips", 0),
                    sl_pips=trade.get("sl_pips", 0),
                    tp1_pips=trade.get("tp1_pips"),
                    tp2_pips=trade.get("tp2_pips"),
                )
                signals.append(sig)

        # 4. Resolve outcomes (walk forward to find TP/SL hits)
        signals = resolve_outcomes(signals, ohlcv_ob_full, cost_model=self.cost_model)

        wins = sum(1 for s in signals if s.outcome == "WIN_TP1")
        losses = sum(1 for s in signals if s.outcome == "LOSS")
        logger.info(
            "Backtest %s done — %d signals (%d WIN, %d LOSS) | "
            "Diag: %d OBs, %d confluence, %d returns, %d hors KZ, %d low-RR",
            pair, len(signals), wins, losses,
            diag_obs_found, diag_obs_confluence, diag_price_returns, diag_filtered_kz,
            diag_filtered_rr,
        )
        return (signals, diag_obs_found, diag_obs_confluence,
                diag_price_returns, diag_filtered_kz, diag_filtered_rr)
