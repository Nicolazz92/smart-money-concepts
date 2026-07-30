"""Main scanner — runs every N seconds, orchestrates the full strategy pipeline."""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from zoneinfo import ZoneInfo

from bot.config import EnvSettings, StrategyConfig
from bot.data_providers import DataProvider, create_provider
from bot.notifier.telegram import TelegramNotifier
from bot.state import BotState
from bot.strategy.bias import get_bias
from bot.strategy.killzone import is_in_killzone
from bot.strategy.ob_scanner import detect_price_return, find_active_obs
from bot.strategy.trade_calculator import calculate_trade

logger = logging.getLogger(__name__)


class Scanner:
    """Orchestrates the strategy loop: bias → OB → detect → notify."""

    def __init__(
        self,
        env: EnvSettings,
        strategy: StrategyConfig,
        state: BotState,
    ):
        self.env = env
        self.strategy = strategy
        self.state = state
        self.provider: DataProvider = create_provider(env)
        self.notifier = TelegramNotifier(
            token=env.telegram_bot_token,
            chat_id=env.telegram_chat_id,
            timezone=env.timezone,
        )
        self.tz = ZoneInfo(env.timezone)
        self.running = False

        # Single-thread executor: MT5 IPC only works when ALL calls
        # (initialize, login, copy_rates…) happen in the same thread.
        self._data_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="data"
        )

        # Live state exposed to the UI
        self.current_biases: dict[str, dict] = {}
        self.active_obs: list[dict] = []
        self.last_scan_time: str = ""
        self.last_error: str = ""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> bool:
        # Connect inside the dedicated data thread (MT5 thread-safety)
        future = self._data_executor.submit(self.provider.connect)
        if not future.result(timeout=30):
            self.last_error = "Data provider connection failed"
            logger.error(self.last_error)
            return False
        self.running = True
        logger.info("Scanner started")
        return True

    def stop(self) -> None:
        self.running = False
        self._data_executor.submit(self.provider.disconnect).result(timeout=10)
        logger.info("Scanner stopped")

    # ------------------------------------------------------------------
    # Single scan cycle (called by UI timer or loop)
    # ------------------------------------------------------------------
    async def scan_once(self) -> None:
        if not self.running:
            return

        self.active_obs.clear()
        self.last_scan_time = datetime.now(self.tz).strftime("%H:%M:%S")
        logger.info("Scan cycle started — %s", self.last_scan_time)

        for pair in self.strategy.pairs:
            try:
                await self._scan_pair(pair)
            except Exception as exc:
                self.last_error = f"{pair}: {exc}"
                logger.exception("Error scanning %s", pair)

        logger.info("Scan cycle done — %d active OBs", len(self.active_obs))

        # Housekeeping: remove old dedup entries
        await self.state.clear_old_notifications(days=7)

    # ------------------------------------------------------------------
    def _fetch_pair_data(self, pair: str):
        """Sync helper — runs inside the dedicated data thread.
        Reconnects automatically if the provider connection dropped."""

        # Generic liveness check — works for all providers
        if hasattr(self.provider, 'is_alive'):
            if not self.provider.is_alive():
                logger.warning("Provider connection lost, reconnecting...")
                self.provider.connect()
        elif hasattr(self.provider, '_connected'):
            # Fallback for MT5 (no is_alive method)
            try:
                import MetaTrader5 as mt5
                info = mt5.account_info()
                if info is None:
                    logger.warning("MT5 connection lost, reconnecting...")
                    self.provider.connect()
            except ImportError:
                pass  # Not MT5, skip

        ohlcv_bias = self.provider.get_ohlcv(
            pair, self.strategy.bias_timeframe, 200
        )
        ohlcv_ob = self.provider.get_ohlcv(
            pair, self.strategy.ob_timeframe, 500
        )
        return ohlcv_bias, ohlcv_ob

    async def _scan_pair(self, pair: str) -> None:
        loop = asyncio.get_event_loop()

        # 1. Fetch data in the dedicated data thread (MT5-safe)
        ohlcv_bias, ohlcv_ob = await loop.run_in_executor(
            self._data_executor, self._fetch_pair_data, pair
        )

        # 2. Determine bias on the HTF
        bias = get_bias(ohlcv_bias, swing_length=self.strategy.bias_swing_length)
        self.current_biases[pair] = bias

        if bias["direction"] == 0:
            logger.debug("%s — no clear bias, skipping", pair)
            return

        # 3. Find OBs in confluence with bias
        active_obs, swings = find_active_obs(
            ohlcv_ob,
            bias["direction"],
            swing_length=self.strategy.ob_swing_length,
        )

        # Expose active OBs to the UI
        for ob_idx, ob_row in active_obs.iterrows():
            ob_time = ohlcv_ob.index[int(ob_idx)] if int(ob_idx) < len(ohlcv_ob) else None
            self.active_obs.append(
                {
                    "pair": pair,
                    "direction": bias["direction"],
                    "top": float(ob_row["Top"]),
                    "bottom": float(ob_row["Bottom"]),
                    "strength": float(ob_row["Percentage"]),
                    "ob_time": ob_time,
                }
            )

        # 4. Kill zone filter
        now = datetime.now(self.tz)
        if not is_in_killzone(now, self.strategy.killzones):
            logger.debug("%s — hors kill zone, skip", pair)
            return

        # 5. Detect price return into OB zone
        triggered = detect_price_return(ohlcv_ob, active_obs, bias["direction"])
        if not triggered:
            return

        # 6. Process each triggered OB
        for ob in triggered:
            # Dedup check
            already = await self.state.is_notified(
                pair, ob["ob_index"], ob["top"], ob["bottom"]
            )
            if already:
                continue

            # 6. Calculate trade info
            trade = calculate_trade(
                symbol=pair,
                bias_direction=bias["direction"],
                ob=ob,
                swings=swings,
                ohlcv_ob_tf=ohlcv_ob,
                ohlcv_bias_tf=ohlcv_bias,
                sl_buffer_pips=self.strategy.sl_buffer_pips,
            )

            # 7. Send notification (R:R is informational, no filter)
            trade["signal_time"] = ohlcv_ob.index[-1]
            trade["ob_time"] = ob.get("ob_time")
            self.notifier.send_signal(trade, bias)

            # 8. Persist
            await self.state.mark_notified(
                pair, ob["ob_index"], ob["top"], ob["bottom"], bias["direction"]
            )
            await self.state.save_signal(trade, bias["type"])

            logger.info(
                "SIGNAL %s %s — Entry=%s  SL=%s  TP1=%s",
                pair,
                "BUY" if bias["direction"] == 1 else "SELL",
                trade["entry"],
                trade["sl"],
                trade.get("tp1"),
            )
