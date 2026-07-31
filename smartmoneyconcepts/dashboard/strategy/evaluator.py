"""Fixed StrategyEvaluator — no lookahead bias, with cost model.

Fixes vs upstream:
1. Entry on NEXT candle's open (not current candle's close)
2. TP/SL checked from the candle AFTER entry (not same candle)
3. Same-candle TP+SL → LOSS (conservative, no tick data)
4. Cost model: round-trip commission deducted from PnL
"""

from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

from ..db.base import Candle
from .models import Strategy, Condition, ExitCondition, Trade


@dataclass
class _Position:
    trade: Trade
    entry_index: int
    sl_price: float
    tp_price: float
    bars_held: int = 0
    supertrend_was_direction: Optional[str] = None


SESSION_RANGES = {
    "Asian": (0, 9),
    "London": (8, 17),
    "NewYork": (13, 22),
    "MOEX": (7, 16),  # MOEX main session 10:00-19:00 MSK = 07:00-16:00 UTC
}


def _is_session(
    candle_time: datetime, session: str, window: Optional[list[int]] = None
) -> bool:
    if window and len(window) == 2:
        ranges = (window[0], window[1])
    else:
        ranges = SESSION_RANGES.get(session)
    if not ranges:
        return False
    h = candle_time.hour
    in_session = ranges[0] <= h < ranges[1]
    return in_session


class StrategyEvaluator:
    """Evaluate a YAML strategy on candles.

    Fixed version: no lookahead bias, cost-aware.

    Parameters
    ----------
    cost_pct : float
        Round-trip cost as % of position value (e.g. 0.135 for Bybit perp).
    """

    def __init__(self, cost_pct: float = 0.0):
        self.cost_pct = cost_pct

    def run(
        self,
        strategy: Strategy,
        candles: list[Candle],
        indicators: dict,
    ) -> list[Trade]:
        positions: list[_Position] = []
        closed: list[Trade] = []
        pending_entries: list[tuple[str, int]] = []  # (side, signal_index)

        for i in range(len(candles)):
            candle = candles[i]

            # ── Execute pending entries at this candle's open ──
            if pending_entries and i > 0:
                for side, sig_idx in pending_entries:
                    if len(positions) < strategy.risk.max_positions:
                        pos = self._open_position(
                            strategy, candle, i, side, indicators, sig_idx
                        )
                        if pos:
                            positions.append(pos)
                pending_entries.clear()

            # ── Check exits on open positions (from entry_index + 1) ──
            for pos in list(positions):
                pos.bars_held += 1
                if i > pos.entry_index:  # only check AFTER entry candle
                    if self._check_exit(pos, candle, strategy, indicators, i):
                        if pos.trade.exit_index is not None:
                            closed.append(pos.trade)
                            positions.remove(pos)
                            continue
                # Update trailing stop
                self._update_trailing(pos, candle, strategy)

            # ── Check entries: signal detected on THIS candle, execute on NEXT ──
            if len(positions) + len(pending_entries) < strategy.risk.max_positions:
                side = self._check_entry(candle, strategy, indicators, i, candles)
                if side:
                    # Queue entry for next candle (no lookahead)
                    pending_entries.append((side, i))

        # Close any remaining positions at last candle
        for pos in positions:
            pos.trade.exit_index = len(candles) - 1
            pos.trade.exit_time = candles[-1].timestamp
            pos.trade.exit_price = candles[-1].close
            pos.trade.exit_reason = "end_of_data"
            pos.trade.pnl = _calc_pnl(pos.trade, self.cost_pct)
            pos.trade.status = "closed"
            closed.append(pos.trade)

        return closed

    def _check_entry(
        self,
        candle: Candle,
        strategy: Strategy,
        indicators: dict,
        idx: int,
        candles: list[Candle],
    ) -> Optional[str]:
        bullish_count = 0
        bearish_count = 0

        for cond in strategy.entry_conditions:
            result = _evaluate_condition(cond, candle, indicators, idx, candles)
            if result == "bullish":
                bullish_count += 1
            elif result == "bearish":
                bearish_count += 1

        if bullish_count == len(strategy.entry_conditions):
            return "buy"
        if bearish_count == len(strategy.entry_conditions):
            return "sell"
        return None

    def _open_position(
        self,
        strategy: Strategy,
        candle: Candle,
        idx: int,
        side: str,
        indicators: dict,
        signal_idx: int,
    ) -> Optional[_Position]:
        # FIX: entry at candle OPEN (not close) — realistic execution
        entry_price = candle.open
        if entry_price <= 0:
            return None

        sl_pct = strategy.risk.position_size_pct * 0.01
        has_trend_exit = any(e.type == "trend_exit" for e in strategy.exit_conditions)

        if side == "buy":
            if has_trend_exit:
                swings = indicators.get("swings", {})
                lows = swings.get("Low", [])
                recent_low = None
                for i in range(signal_idx, max(signal_idx - 20, -1), -1):
                    if i < len(lows) and lows[i] is not None and lows[i] != 0:
                        recent_low = lows[i]
                        break
                sl_price = recent_low * 0.995 if recent_low else candle.low * 0.99
            else:
                ob_bottom = _last_ob_boundary(indicators, signal_idx, "bottom") or (
                    candle.low * 0.99
                )
                sl_price = ob_bottom
            tp_ratio = self._get_tp_ratio(strategy)
            tp_price = entry_price + (entry_price - sl_price) * tp_ratio
        else:
            if has_trend_exit:
                swings = indicators.get("swings", {})
                highs = swings.get("High", [])
                recent_high = None
                for i in range(signal_idx, max(signal_idx - 20, -1), -1):
                    if i < len(highs) and highs[i] is not None and highs[i] != 0:
                        recent_high = highs[i]
                        break
                sl_price = recent_high * 1.005 if recent_high else candle.high * 1.01
            else:
                ob_top = _last_ob_boundary(indicators, signal_idx, "top") or (
                    candle.high * 1.01
                )
                sl_price = ob_top
            tp_ratio = self._get_tp_ratio(strategy)
            tp_price = entry_price - (sl_price - entry_price) * tp_ratio

        qty = 1000 * sl_pct / entry_price
        trade = Trade(
            strategy=strategy.name,
            side=side,
            entry_index=idx,
            entry_time=candle.timestamp,
            entry_price=entry_price,
            quantity=qty,
        )
        return _Position(
            trade=trade, entry_index=idx, sl_price=sl_price, tp_price=tp_price
        )

    def _get_tp_ratio(self, strategy: Strategy) -> float:
        for e in strategy.exit_conditions:
            if e.type == "target":
                return e.value
        return 1.5

    def _track_supertrend(self, pos: _Position, indicators: dict, idx: int):
        st = indicators.get("supertrend", {})
        trend = st.get("trend", [])
        if idx < len(trend) and trend[idx] is not None:
            if trend[idx] == 1 and pos.supertrend_was_direction is None:
                pos.supertrend_was_direction = "bullish"
            elif trend[idx] == -1 and pos.supertrend_was_direction is None:
                pos.supertrend_was_direction = "bearish"

    def _check_exit(
        self,
        pos: _Position,
        candle: Candle,
        strategy: Strategy,
        indicators: dict,
        idx: int,
    ) -> bool:
        self._track_supertrend(pos, indicators, idx)

        # FIX: check TP and SL on same candle → conservative = LOSS
        tp_hit = False
        sl_hit = False
        exit_price_tp = None
        exit_price_sl = None

        for e in strategy.exit_conditions:
            if e.type == "trend_exit":
                st = indicators.get("supertrend", {})
                st_trend = st.get("trend", [])
                st_val = st_trend[idx] if idx < len(st_trend) else None
                ema_21 = indicators.get("ema_21", [])
                ema_val = ema_21[idx] if idx < len(ema_21) else None

                if pos.trade.side == "buy":
                    if pos.supertrend_was_direction == "bullish" and st_val == -1:
                        pos.trade.exit_index = idx
                        pos.trade.exit_time = candle.timestamp
                        pos.trade.exit_price = candle.close
                        pos.trade.exit_reason = "supertrend_flip"
                        pos.trade.pnl = _calc_pnl(pos.trade, self.cost_pct)
                        pos.trade.status = "closed"
                        return True
                    if (
                        pos.supertrend_was_direction != "bullish"
                        and ema_val is not None
                        and candle.close < ema_val
                    ):
                        pos.trade.exit_index = idx
                        pos.trade.exit_time = candle.timestamp
                        pos.trade.exit_price = candle.close
                        pos.trade.exit_reason = "ema_exit"
                        pos.trade.pnl = _calc_pnl(pos.trade, self.cost_pct)
                        pos.trade.status = "closed"
                        return True
                else:
                    if pos.supertrend_was_direction == "bearish" and st_val == 1:
                        pos.trade.exit_index = idx
                        pos.trade.exit_time = candle.timestamp
                        pos.trade.exit_price = candle.close
                        pos.trade.exit_reason = "supertrend_flip"
                        pos.trade.pnl = _calc_pnl(pos.trade, self.cost_pct)
                        pos.trade.status = "closed"
                        return True
                    if (
                        pos.supertrend_was_direction != "bearish"
                        and ema_val is not None
                        and candle.close > ema_val
                    ):
                        pos.trade.exit_index = idx
                        pos.trade.exit_time = candle.timestamp
                        pos.trade.exit_price = candle.close
                        pos.trade.exit_reason = "ema_exit"
                        pos.trade.pnl = _calc_pnl(pos.trade, self.cost_pct)
                        pos.trade.status = "closed"
                        return True

            if e.type == "stop_loss":
                sl = e.value
                if pos.trade.side == "buy":
                    exit_sl = (
                        pos.trade.entry_price
                        - (pos.trade.entry_price - pos.sl_price) * sl
                    )
                    if candle.low <= exit_sl:
                        sl_hit = True
                        exit_price_sl = exit_sl
                else:
                    exit_sl = (
                        pos.trade.entry_price
                        + (pos.sl_price - pos.trade.entry_price) * sl
                    )
                    if candle.high >= exit_sl:
                        sl_hit = True
                        exit_price_sl = exit_sl

            if e.type == "target":
                tp_ratio = e.value
                if pos.trade.side == "buy":
                    tp_price = (
                        pos.trade.entry_price
                        + (pos.trade.entry_price - pos.sl_price) * tp_ratio
                    )
                    if candle.high >= tp_price:
                        tp_hit = True
                        exit_price_tp = tp_price
                else:
                    tp_price = (
                        pos.trade.entry_price
                        - (pos.sl_price - pos.trade.entry_price) * tp_ratio
                    )
                    if candle.low <= tp_price:
                        tp_hit = True
                        exit_price_tp = tp_price

        # FIX: both TP and SL on same candle → conservative = LOSS
        if sl_hit and tp_hit:
            pos.trade.exit_index = idx
            pos.trade.exit_time = candle.timestamp
            pos.trade.exit_price = exit_price_sl  # assume SL hit first
            pos.trade.exit_reason = "stop_loss"
            pos.trade.pnl = _calc_pnl(pos.trade, self.cost_pct)
            pos.trade.status = "closed"
            return True
        elif sl_hit:
            pos.trade.exit_index = idx
            pos.trade.exit_time = candle.timestamp
            pos.trade.exit_price = exit_price_sl
            pos.trade.exit_reason = "stop_loss"
            pos.trade.pnl = _calc_pnl(pos.trade, self.cost_pct)
            pos.trade.status = "closed"
            return True
        elif tp_hit:
            pos.trade.exit_index = idx
            pos.trade.exit_time = candle.timestamp
            pos.trade.exit_price = exit_price_tp
            pos.trade.exit_reason = "target"
            pos.trade.pnl = _calc_pnl(pos.trade, self.cost_pct)
            pos.trade.status = "closed"
            return True

        return False

    def _update_trailing(self, pos: _Position, candle: Candle, strategy: Strategy):
        for e in strategy.exit_conditions:
            if e.type == "trailing_stop" and e.trail_activation:
                if pos.trade.side == "buy":
                    move = candle.high - pos.trade.entry_price
                    if move >= pos.trade.entry_price * e.trail_activation * 0.01:
                        pos.sl_price = max(pos.sl_price, candle.high - move * 0.5)
                else:
                    move = pos.trade.entry_price - candle.low
                    if move >= pos.trade.entry_price * e.trail_activation * 0.01:
                        pos.sl_price = min(pos.sl_price, candle.low + move * 0.5)


def _calc_pnl(trade: Trade, cost_pct: float = 0.0) -> float:
    if trade.exit_price is None or trade.entry_price == 0:
        return 0.0
    diff = trade.exit_price - trade.entry_price
    if trade.side == "sell":
        diff = -diff
    gross_pnl = diff * trade.quantity
    # Deduct round-trip cost
    cost = trade.entry_price * trade.quantity * (cost_pct / 100)
    return gross_pnl - cost


def _last_ob_boundary(indicators: dict, idx: int, boundary: str) -> Optional[float]:
    ob = indicators.get("ob", {})
    vals = ob.get("Top" if boundary == "top" else "Bottom", [])
    for i in range(idx, max(idx - 5, -1), -1):
        if i < len(vals) and vals[i] is not None:
            return vals[i]
    return None


def _evaluate_condition(
    cond: Condition,
    candle: Candle,
    indicators: dict,
    idx: int,
    candles: list[Candle],
) -> Optional[str]:
    lookback = cond.params.get("lookback", 5)

    if cond.type == "fvg_mitigation":
        fvg = indicators.get("fvg", {})
        vals = fvg.get("FVG", [])
        tops = fvg.get("Top", [])
        bottoms = fvg.get("Bottom", [])

        for i in range(idx, max(idx - lookback, -1), -1):
            if i >= len(vals) or vals[i] is None or vals[i] == 0:
                continue
            direction = vals[i]
            top = tops[i] if i < len(tops) else None
            bottom = bottoms[i] if i < len(bottoms) else None
            if top is None or bottom is None:
                continue

            if direction == 1:  # bullish FVG
                if candle.low <= top and candle.close > bottom:
                    return "bullish"
            elif direction == -1:  # bearish FVG
                if candle.high >= bottom and candle.close < top:
                    return "bearish"

    elif cond.type == "ob_break":
        ob = indicators.get("ob", {})
        vals = ob.get("OB", [])
        tops = ob.get("Top", [])
        bottoms = ob.get("Bottom", [])

        for i in range(idx, max(idx - lookback, -1), -1):
            if i >= len(vals) or vals[i] is None or vals[i] == 0:
                continue
            direction = vals[i]
            top = tops[i] if i < len(tops) else None
            bottom = bottoms[i] if i < len(bottoms) else None
            if top is None or bottom is None:
                continue

            if direction == 1 and candle.close > top:
                return "bullish"
            elif direction == -1 and candle.close < bottom:
                return "bearish"

    elif cond.type == "liquidity_sweep":
        liq = indicators.get("liquidity", {})
        vals = liq.get("Liquidity", [])
        if idx < len(vals) and vals[idx] is not None and vals[idx] != 0:
            return cond.direction

    elif cond.type == "bos":
        bos = indicators.get("bos_choch", {}).get("BOS", [])
        if idx < len(bos) and bos[idx] is not None and bos[idx] != 0:
            return "bullish" if bos[idx] == 1 else "bearish"

    elif cond.type == "choch":
        choch = indicators.get("bos_choch", {}).get("CHOCH", [])
        if idx < len(choch) and choch[idx] is not None and choch[idx] != 0:
            return "bullish" if choch[idx] == 1 else "bearish"

    elif cond.type == "session":
        session = cond.params.get("session", "")
        window = cond.params.get("window")
        if _is_session(candle.timestamp, session, window):
            return cond.direction

    elif cond.type == "trend":
        swings = indicators.get("swings", {})
        highs = swings.get("High", [])
        lows = swings.get("Low", [])

        recent_highs = [
            h
            for h in highs[max(0, idx - lookback) : idx + 1]
            if h is not None and h != 0
        ]
        recent_lows = [
            l
            for l in lows[max(0, idx - lookback) : idx + 1]
            if l is not None and l != 0
        ]

        if cond.direction == "bullish" and len(recent_highs) >= 2:
            return "bullish"
        if cond.direction == "bearish" and len(recent_lows) >= 2:
            return "bearish"

    elif cond.type == "vwap":
        vwap = indicators.get("vwap", [])
        if idx < len(vwap) and vwap[idx] is not None:
            if cond.direction == "bullish" and candle.close > vwap[idx]:
                return "bullish"
            elif cond.direction == "bearish" and candle.close < vwap[idx]:
                return "bearish"

    elif cond.type == "ema":
        period = cond.params.get("period", 9)
        key = f"ema_{period}"
        vals = indicators.get(key, [])
        if idx < len(vals) and vals[idx] is not None:
            if cond.direction == "bullish" and candle.close > vals[idx]:
                return "bullish"
            elif cond.direction == "bearish" and candle.close < vals[idx]:
                return "bearish"

    elif cond.type == "donchian_trend":
        don = indicators.get("donchian", {})
        trend = don.get("trend", [])
        if idx < len(trend) and trend[idx] is not None:
            if cond.direction == "bullish" and trend[idx] == 1:
                return "bullish"
            elif cond.direction == "bearish" and trend[idx] == -1:
                return "bearish"

    elif cond.type == "hull_suite":
        hull = indicators.get("hull", {})
        direction = hull.get("direction", [])
        if idx < len(direction) and direction[idx] is not None:
            if cond.direction == "bullish" and direction[idx] == 1:
                return "bullish"
            elif cond.direction == "bearish" and direction[idx] == -1:
                return "bearish"

    return None
