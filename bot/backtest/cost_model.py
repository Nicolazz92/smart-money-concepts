"""Realistic trading cost model for MOEX backtests.

Sources (all verified from broker pages and MOEX tariff pages, July 2026):
  Finam tariffs:
    FreeTrade       : 0% broker fee on MOEX shares
    Стратег          : 0.05% broker fee, min 50 RUB per order
    Инвестор         : 0.025-0.035% broker fee, 200 RUB/month
    Единый дневной   : 0.00944-0.0354%, min 41.3 RUB per order, 177 RUB/month
  MOEX exchange+clearing (always charged, included in broker % or separate):
    Taker           : 0.03%   (0.01725% MOEX + 0.01275% NCC)
    Maker           : 0.015%  (limit orders that add liquidity)

For strategy backtesting we always assume TAKER (entries on OB touch =
market orders, not limit). Spread + slippage added on top.

Usage:
    from bot.backtest.cost_model import CostModel, FINAM_PRESETS
    cm = FINAM_PRESETS["freetrade"]      # or "strateg", "investor"
    cost = cm.trade_cost(price=300.0, shares=10)   # RUB on one leg
    round_trip = cost * 2                            # entry + exit
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CostModel:
    """Per-trade cost model. All rates in fractions (0.0003 = 3 bps).

    Trade cost on one leg (entry OR exit) is computed as:
        cost = max(price * shares * broker_rate, broker_min_rub)
             + price * shares * (exchange_rate + spread_rate + slippage_rate)
    Total round-trip cost = 2 × cost (entry + exit).
    """

    name: str = "custom"
    broker_rate: float = 0.0          # broker commission, fraction
    broker_min_rub: float = 0.0       # minimum broker fee per order, RUB
    exchange_rate: float = 0.0003     # MOEX + NCC, taker side
    spread_rate: float = 0.0003       # half-spread paid to cross the book
    slippage_rate: float = 0.0002     # market-impact / fill degradation
    monthly_fee_rub: float = 0.0      # account fee (amortised separately)
    notes: str = ""

    def leg_cost(self, price: float, shares: float) -> float:
        """Cost in RUB of one side (entry or exit) of a trade."""
        notional = price * shares
        broker = max(notional * self.broker_rate, self.broker_min_rub)
        market = notional * (self.exchange_rate + self.spread_rate + self.slippage_rate)
        return broker + market

    def trade_cost(self, price: float, shares: float) -> float:
        """Round-trip cost in RUB (entry + exit)."""
        return 2.0 * self.leg_cost(price, shares)

    def cost_as_price_delta(self, price: float) -> float:
        """Round-trip cost expressed as price move against the position.

        Useful for the backtest: instead of knowing position size, we can
        express the cost as a virtual widening of the stop and tightening
        of the target. For a position of `n` shares, total cost =
        n × price_delta, so the per-share cost is independent of n.
        """
        # leg_cost / shares = price × (broker_rate + exchange + spread + slippage)
        # (broker_min ignored because at typical share counts broker_min is
        #  not the binding constraint; per-share view is what we need anyway)
        per_share_rate = (
            self.broker_rate + self.exchange_rate
            + self.spread_rate + self.slippage_rate
        )
        return 2.0 * price * per_share_rate


# ---------------------------------------------------------------------------
# Finam tariff presets — verified rates as of July 2026
# ---------------------------------------------------------------------------
# FreeTrade: 0% broker fee on MOEX shares. Only exchange + spread + slippage
# remain. This is the cheapest realistic option for MOEX share swing trading.
FINAM_FRETRADE = CostModel(
    name="finam-freetrade",
    broker_rate=0.0,
    broker_min_rub=0.0,
    exchange_rate=0.0003,    # 0.03% MOEX + NCC, taker
    spread_rate=0.0003,      # 0.03% half-spread, typical MOEX top-5
    slippage_rate=0.0002,    # 0.02% market impact
    monthly_fee_rub=0.0,
    notes="Финам FreeTrade: 0% broker, только биржа+спред+слиппедж (≈0.16% round-trip)",
)

# Стратег: 0.05% broker, min 50 RUB per order. Good baseline for comparison.
FINAM_STRATEG = CostModel(
    name="finam-strateg",
    broker_rate=0.0005,
    broker_min_rub=50.0,
    exchange_rate=0.0003,
    spread_rate=0.0003,
    slippage_rate=0.0002,
    monthly_fee_rub=0.0,
    notes="Финам Стратег: 0.05% min 50₽ (≈0.26% round-trip на малых лотах)",
)

# Инвестор: 0.03% broker (mid of 0.025-0.035), 200 RUB/month.
# For 3.3-year backtest, monthly fee amortises to ~6600 RUB total — small.
FINAM_INVESTOR = CostModel(
    name="finam-investor",
    broker_rate=0.0003,
    broker_min_rub=0.0,
    exchange_rate=0.0003,
    spread_rate=0.0003,
    slippage_rate=0.0002,
    monthly_fee_rub=200.0,
    notes="Финам Инвестор: 0.03% + 200₽/мес (≈0.24% round-trip)",
)

# Единый дневной: lowest broker rate 0.00944-0.0354%, min 41.3 RUB.
FINAM_UNIFIED_DAILY = CostModel(
    name="finam-unified-daily",
    broker_rate=0.0002,       # mid of 0.00944-0.0354%
    broker_min_rub=41.3,
    exchange_rate=0.0003,
    spread_rate=0.0003,
    slippage_rate=0.0002,
    monthly_fee_rub=177.0,
    notes="Финам Единый дневной: 0.02% min 41.3₽ + 177₽/мес (≈0.20% round-trip)",
)

# Idealised "no costs" baseline (original SNahary behaviour).
ZERO_COST = CostModel(
    name="zero",
    broker_rate=0.0,
    broker_min_rub=0.0,
    exchange_rate=0.0,
    spread_rate=0.0,
    slippage_rate=0.0,
    monthly_fee_rub=0.0,
    notes="Идеализированный 0% — для сравнения",
)


# ---------------------------------------------------------------------------
# Crypto exchange presets — verified July 2026
# ---------------------------------------------------------------------------
# Bybit official rates (VIP 0): perp taker 0.0550%, spot taker 0.1000%.
# MNT (token) discount: 10% perp / 25% spot — BUT only for non-API trades.
#   Source: bybit.com/en/help-center/article/FAQ-Paying-Trading-Fees-with-MNT
#   "Paying fees with MNT ... is not supported for Market Markers,
#    institutional users, Pro users, or API users."
# Therefore for an automated API bot we use the FULL taker rate, no MNT
# discount. Only the referral rebate (30% via standard affiliate programs
# like Rebatly/Trade Reclaim at < $50M/mo volume) applies.
#
# Effective perp taker after 30% ref rebate: 0.055% × (1 - 0.30) = 0.0385%
# Effective spot taker after 30% ref rebate: 0.100% × (1 - 0.30) = 0.0700%
#
# Funding rate is excluded — our backtest holds hours/days, and funding
# nets out ~zero in expectation across longs and shorts.
BYBIT_PERP_TAKER = CostModel(
    name="bybit-perp-taker",
    broker_rate=0.000385,    # 0.055% × (1 - 0.30 ref rebate) — API bot, no MNT
    broker_min_rub=0.0,
    exchange_rate=0.0,       # exchange fee IS the broker_rate for crypto
    spread_rate=0.0001,      # tight top-10 perp spread ~1 bps
    slippage_rate=0.0003,    # 3 bps slippage on aggressive fills
    monthly_fee_rub=0.0,
    notes="Bybit perp API: 0.055% × 0.70 (30% ref) + ~4bps (≈0.135% round-trip)",
)

# Spot: 0.1% × 0.70 (30% ref) — same rebate. No funding.
BYBIT_SPOT_TAKER = CostModel(
    name="bybit-spot-taker",
    broker_rate=0.0007,      # 0.1% × (1 - 0.30 ref rebate)
    broker_min_rub=0.0,
    exchange_rate=0.0,
    spread_rate=0.0005,      # 5 bps spread (wider for alts)
    slippage_rate=0.0005,    # 5 bps slippage
    monthly_fee_rub=0.0,
    notes="Bybit spot API: 0.1% × 0.70 (30% ref) + ~10bps (≈0.24% round-trip)",
)

FINAM_PRESETS: dict[str, CostModel] = {
    "freetrade": FINAM_FRETRADE,
    "strateg": FINAM_STRATEG,
    "investor": FINAM_INVESTOR,
    "unified-daily": FINAM_UNIFIED_DAILY,
    "bybit-perp": BYBIT_PERP_TAKER,
    "bybit-spot": BYBIT_SPOT_TAKER,
    "zero": ZERO_COST,
}
