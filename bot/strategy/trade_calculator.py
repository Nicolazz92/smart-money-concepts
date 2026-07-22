"""Calculate complete trade info (Entry, SL, TP1, TP2, R:R) from a triggered OB."""

import logging

import numpy as np
import pandas as pd
from smartmoneyconcepts import smc

logger = logging.getLogger(__name__)

# Approximate pip values per symbol (used only for display).
_PIP_SIZE = {
    "XAUUSD": 0.1,      # gold: 1 pip = 0.1
    # MOEX shares — chosen so that 1 pip ≈ 1 basis point of typical price
    # move. These drive the sl_buffer_pips setting in strategy config.
    "SBER": 0.01,       # ~100 RUB
    "GAZP": 0.01,       # ~150 RUB
    "GMKN": 0.1,        # ~7000 RUB (large price)
    "ROSN": 0.01,       # ~250 RUB
    "LKOH": 0.1,        # ~1900 RUB (large price)
    "IMOEX": 0.01,      # index points (~3000)
}
_DEFAULT_PIP = 0.0001   # forex majors: 1 pip = 0.0001


def _pip_size(symbol: str) -> float:
    # Crypto perp / spot on Bybit — derive pip from symbol suffix.
    # "BTCUSDT" -> 0.01, "ETHUSDT" -> 0.01, "1000PEPEUSDT" -> 0.0001
    # (The "1000" prefix tokens are scaled; underlying is divided by 1000.)
    if symbol.endswith("USDT") or symbol.endswith("PERP"):
        if symbol.startswith("1000"):
            return 0.0001
        return 0.01
    return _PIP_SIZE.get(symbol, _DEFAULT_PIP)


def _to_pips(value: float, symbol: str) -> float:
    return round(value / _pip_size(symbol), 1)


def calculate_trade(
    symbol: str,
    bias_direction: int,
    ob: dict,
    swings: pd.DataFrame,
    ohlcv_ob_tf: pd.DataFrame,
    ohlcv_bias_tf: pd.DataFrame,
    sl_buffer_pips: float = 5.0,
) -> dict:
    """Build a complete trade signal from a triggered OB.

    Parameters
    ----------
    symbol         : pair name (e.g. "EURUSD")
    bias_direction : 1 (bullish) or -1 (bearish)
    ob             : dict with keys top, bottom, percentage, volume, ob_index
    swings         : swing_highs_lows result from the OB timeframe
    ohlcv_ob_tf    : OHLCV DataFrame of the OB timeframe
    ohlcv_bias_tf  : OHLCV DataFrame of the bias (HTF) timeframe
    sl_buffer_pips : extra margin below/above SL in pips

    Returns
    -------
    dict with: entry, sl, tp1, tp2, rr1, rr2, ob_size_pips, ob_strength, direction
    """
    pip = _pip_size(symbol)
    buffer = sl_buffer_pips * pip

    ob_top = ob["top"]
    ob_bottom = ob["bottom"]
    ob_size_pips = _to_pips(ob_top - ob_bottom, symbol)

    swing_levels = swings["Level"].dropna()
    swing_types = swings["HighLow"].dropna()

    # ------------------------------------------------------------------
    # HTF previous high/low — used as TP2
    # ------------------------------------------------------------------
    prev_hl = smc.previous_high_low(ohlcv_ob_tf, time_frame=_htf_resample(ohlcv_bias_tf))
    prev_high = _last_valid(prev_hl, "PreviousHigh")
    prev_low = _last_valid(prev_hl, "PreviousLow")

    if bias_direction == 1:
        # --- BULLISH ---
        entry = ob_top
        sl = ob_bottom - buffer

        # TP1: nearest swing high above entry (from OB TF)
        highs_above = swing_levels[(swing_types == 1) & (swing_levels > entry)]
        tp1 = float(highs_above.min()) if not highs_above.empty else None

        # TP2: previous high from bias TF
        tp2 = prev_high if prev_high is not None and prev_high > entry else None
    else:
        # --- BEARISH ---
        entry = ob_bottom
        sl = ob_top + buffer

        # TP1: nearest swing low below entry (from OB TF)
        lows_below = swing_levels[(swing_types == -1) & (swing_levels < entry)]
        tp1 = float(lows_below.max()) if not lows_below.empty else None

        # TP2: previous low from bias TF
        tp2 = prev_low if prev_low is not None and prev_low < entry else None

    # ------------------------------------------------------------------
    # Risk:Reward
    # ------------------------------------------------------------------
    risk = abs(entry - sl)
    # Minimum risk guard: if OB zone is near-zero width (data noise /split-adjusted),
    # SL becomes ~equal to entry → absurd R:R (e.g. 720:1).
    # Force a minimum risk of 5 pips to keep R:R realistic.
    min_risk = 5 * pip
    if risk < min_risk:
        # Widen SL to enforce minimum risk
        if bias_direction == 1:
            sl = entry - min_risk
        else:
            sl = entry + min_risk
        risk = min_risk
    rr1 = round(abs(tp1 - entry) / risk, 1) if tp1 and risk > 0 else None
    rr2 = round(abs(tp2 - entry) / risk, 1) if tp2 and risk > 0 else None
    # Cap R:R at 20:1 — anything higher is data noise, not real edge
    if rr1 is not None and rr1 > 20:
        rr1 = 20.0
    if rr2 is not None and rr2 > 20:
        rr2 = 20.0

    trade = {
        "symbol": symbol,
        "direction": bias_direction,
        "entry": round(entry, 5),
        "sl": round(sl, 5),
        "tp1": round(tp1, 5) if tp1 else None,
        "tp2": round(tp2, 5) if tp2 else None,
        "rr1": rr1,
        "rr2": rr2,
        "sl_pips": _to_pips(risk, symbol),
        "tp1_pips": _to_pips(abs(tp1 - entry), symbol) if tp1 else None,
        "tp2_pips": _to_pips(abs(tp2 - entry), symbol) if tp2 else None,
        "ob_size_pips": ob_size_pips,
        "ob_strength": round(ob["percentage"], 1),
    }

    logger.debug("Trade calculated: %s", trade)
    return trade


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _last_valid(df: pd.DataFrame, col: str):
    """Return last non-NaN value of a column, or None."""
    idx = df[col].last_valid_index()
    if idx is None:
        return None
    return float(df[col].iloc[idx])


def _htf_resample(ohlcv_bias_tf: pd.DataFrame) -> str:
    """Guess the resample string for previous_high_low from bias TF candle spacing."""
    if len(ohlcv_bias_tf) < 2:
        return "1d"
    idx = ohlcv_bias_tf.index
    if hasattr(idx, "freq") and idx.freq is not None:
        return str(idx.freq)
    # Infer from median interval between candles
    delta = pd.Series(idx).diff().median()
    hours = delta.total_seconds() / 3600
    if hours <= 1:
        return "1h"
    if hours <= 4:
        return "4h"
    if hours <= 24:
        return "1D"
    return "1W"
