"""NiceGUI dashboard — configuration panel + real-time signal monitoring + backtest."""

import asyncio
import logging

from nicegui import app, ui

from bot.config import EnvSettings, StrategyConfig
from bot.scanner import Scanner
from bot.state import BotState
from bot.ui.backtest_panel import build_backtest_panel
from bot.ui.checklist_panel import build_checklist_panel
from bot.ui.journal_panel import build_journal_panel
from bot.ui.killzone_panel import build_killzone_panel

logger = logging.getLogger(__name__)

# Global instances shared across the UI
_env = EnvSettings()
_strategy = StrategyConfig.load()
_state = BotState()
_scanner: Scanner | None = None

# Paper trading portfolio
from ..paper.portfolio import Portfolio
from ..paper.broker import PaperBroker
_paper_portfolio = Portfolio(
    initial_capital=_env.paper_initial_capital,
    position_pct_long=_env.paper_position_pct_long,
    position_pct_short=_env.paper_position_pct_short,
    max_concurrent=_env.paper_max_concurrent,
    cost_pct=_env.paper_cost_pct,
)
_paper_broker = PaperBroker(_paper_portfolio)
_paper_refresh_funcs: list = []

# Available options
PAIRS_OPTIONS = [
    "EURUSD", "GBPUSD", "XAUUSD",
    "USDJPY", "GBPJPY", "EURJPY",
    "AUDUSD", "USDCAD", "USDCHF", "NZDUSD",
    # FORTS futures
    "SR", "GZ", "LK", "MM", "RN", "VB", "MX", "Ri", "Si", "GD",
    # MOEX TQBR shares (top-10)
    "SBER", "GAZP", "LKOH", "GMKN", "ROSN", "VTBR", "NVTK", "MGNT", "MOEX", "AFKS",
]
BIAS_TF_OPTIONS = ["1H", "4H", "1D"]
OB_TF_OPTIONS = ["5M", "15M", "30M", "1H", "4H", "60M"]
TZ_OPTIONS = {
    "UTC": "UTC",
    "Broker (UTC+2/+3 auto DST)": "Europe/Helsinki",
    "Moscow (UTC+3)": "Europe/Moscow",
    "Afrique East / Nairobi (UTC+3)": "Africa/Nairobi",
}
KZ_OPTIONS = [
    "London open kill zone",
    "New York kill zone",
    "London close kill zone",
    "Asian kill zone",
    "MOEX main session",
]


def start_dashboard() -> None:
    """Entry point — called from run.py."""

    # Init DB once at startup, not on every page load
    async def _init_db():
        if _state._db is None:
            await _state.init()

    app.on_startup(_init_db)

    @ui.page("/")
    async def main_page():
        _build_ui()

    ui.run(
        port=8080,
        title="ICT / SMC Signal Bot",
        favicon="📊",
        dark=True,
        reload=False,
    )


# ======================================================================
# UI Layout
# ======================================================================

def _build_ui() -> None:
    # ------------------------------------------------------------------
    # Header — refined dark gradient with logo + status pill + action buttons
    # ------------------------------------------------------------------
    ui.add_head_html("""
    <style>
    .bot-header {
        background: linear-gradient(135deg, #0f172a 0%, #1e293b 50%, #334155 100%) !important;
        box-shadow: 0 2px 8px rgba(0,0,0,0.4);
        border-bottom: 1px solid rgba(255,255,255,0.08);
    }
    .status-pill {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 6px 14px;
        border-radius: 999px;
        font-size: 12px;
        font-weight: 600;
        letter-spacing: 0.5px;
        text-transform: uppercase;
    }
    .status-running {
        background: rgba(34, 197, 94, 0.15);
        color: #22c55e;
        border: 1px solid rgba(34, 197, 94, 0.4);
    }
    .status-stopped {
        background: rgba(148, 163, 184, 0.15);
        color: #94a3b8;
        border: 1px solid rgba(148, 163, 184, 0.3);
    }
    .status-dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: currentColor;
    }
    .status-running .status-dot {
        animation: pulse 1.8s ease-in-out infinite;
        box-shadow: 0 0 0 0 rgba(34, 197, 94, 0.6);
    }
    @keyframes pulse {
        0%, 100% { box-shadow: 0 0 0 0 rgba(34, 197, 94, 0.5); }
        50% { box-shadow: 0 0 0 8px rgba(34, 197, 94, 0); }
    }
    .btn-start {
        background: linear-gradient(135deg, #10b981, #059669) !important;
        color: white !important;
        border-radius: 10px !important;
        font-weight: 600 !important;
        letter-spacing: 0.3px;
        box-shadow: 0 2px 6px rgba(16, 185, 129, 0.35);
    }
    .btn-stop {
        background: linear-gradient(135deg, #ef4444, #dc2626) !important;
        color: white !important;
        border-radius: 10px !important;
        font-weight: 600 !important;
        letter-spacing: 0.3px;
        box-shadow: 0 2px 6px rgba(239, 68, 68, 0.35);
    }
    .btn-start:disabled, .btn-stop:disabled {
        opacity: 0.4 !important;
        box-shadow: none !important;
    }
    </style>
    """)

    with ui.header().classes("bot-header items-center justify-between q-px-lg q-py-sm"):
        # Left: logo + title
        with ui.row().classes("items-center gap-3"):
            ui.icon("candlestick_chart", size="2rem").classes("text-cyan-400")
            with ui.column().classes("gap-0"):
                ui.label("SMC Signal Bot").classes("text-h6 text-weight-bold text-white q-mb-none")
                ui.label("Indicateur ICT / Smart Money Concepts").classes("text-caption text-grey-5")

        # Right: status pill + buttons
        with ui.row().classes("items-center gap-3"):
            status_pill = ui.html('<div class="status-pill status-stopped"><span class="status-dot"></span>Stopped</div>')
            status_label = status_pill  # keep legacy name for other code

            start_btn = ui.button(
                "Start", icon="play_arrow",
                on_click=lambda: _start_scanning(status_label, start_btn, stop_btn),
            ).classes("btn-start q-px-md")
            stop_btn = ui.button(
                "Stop", icon="stop",
                on_click=lambda: _stop_scanning(status_label, start_btn, stop_btn),
            ).classes("btn-stop q-px-md")
            stop_btn.set_enabled(False)

    with ui.splitter(value=25).classes("w-full h-full") as splitter:
        # ==============================================================
        # LEFT: Config panel
        # ==============================================================
        with splitter.before:
            with ui.card().classes("w-full"):
                ui.label("Configuration").classes("text-h6")
                ui.separator()

                # --- Data Provider ---
                ui.label("Data Provider").classes("text-caption text-grey")
                provider_select = ui.select(
                    options=["mt5", "oanda", "csv", "moex"],
                    value=_env.data_provider,
                    on_change=lambda e: setattr(_env, "data_provider", e.value),
                ).classes("w-full")

                # --- Timezone ---
                ui.label("Timezone").classes("text-caption text-grey q-mt-sm")
                tz_select = ui.select(
                    options=list(TZ_OPTIONS.keys()),
                    value=next(
                        (k for k, v in TZ_OPTIONS.items() if v == _env.timezone),
                        "UTC",
                    ),
                    on_change=lambda e: setattr(_env, "timezone", TZ_OPTIONS[e.value]),
                ).classes("w-full")

                ui.separator()

                # --- Pairs ---
                ui.label("Paires").classes("text-caption text-grey")
                pairs_select = ui.select(
                    options=PAIRS_OPTIONS,
                    value=_strategy.pairs,
                    multiple=True,
                    on_change=lambda e: _update_strategy("pairs", e.value),
                ).classes("w-full").props("use-chips")

                # --- Bias TF ---
                ui.label("TF Biais").classes("text-caption text-grey q-mt-sm")
                ui.select(
                    options=BIAS_TF_OPTIONS,
                    value=_strategy.bias_timeframe,
                    on_change=lambda e: _update_strategy("bias_timeframe", e.value),
                ).classes("w-full")

                # --- OB TF ---
                ui.label("TF Order Block").classes("text-caption text-grey q-mt-sm")
                ui.select(
                    options=OB_TF_OPTIONS,
                    value=_strategy.ob_timeframe,
                    on_change=lambda e: _update_strategy("ob_timeframe", e.value),
                ).classes("w-full")

                ui.separator()

                # --- Swing lengths ---
                ui.label("Swing Length (Biais)").classes("text-caption text-grey")
                ui.number(
                    value=_strategy.bias_swing_length,
                    min=3,
                    max=50,
                    step=1,
                    on_change=lambda e: _update_strategy("bias_swing_length", int(e.value)),
                ).classes("w-full")

                ui.label("Swing Length (OB)").classes("text-caption text-grey q-mt-sm")
                ui.number(
                    value=_strategy.ob_swing_length,
                    min=3,
                    max=50,
                    step=1,
                    on_change=lambda e: _update_strategy("ob_swing_length", int(e.value)),
                ).classes("w-full")

                # --- Filters ---
                ui.separator()
                ui.label("Filtres").classes("text-caption text-grey")

                ui.label("Buffer SL (pips)").classes("text-caption text-grey q-mt-sm")
                ui.number(
                    value=_strategy.sl_buffer_pips,
                    min=0,
                    max=50,
                    step=1,
                    format="%.1f",
                    on_change=lambda e: _update_strategy("sl_buffer_pips", float(e.value)),
                ).classes("w-full")

                # --- Kill Zones ---
                ui.separator()
                ui.label("Kill Zones").classes("text-caption text-grey")
                ui.select(
                    options=KZ_OPTIONS,
                    value=_strategy.killzones,
                    multiple=True,
                    on_change=lambda e: _update_strategy("killzones", e.value),
                ).classes("w-full").props("use-chips")

                # --- Scan interval ---
                ui.separator()
                ui.label("Intervalle de scan (sec)").classes("text-caption text-grey")
                ui.number(
                    value=_strategy.scan_interval,
                    min=60,
                    max=3600,
                    step=60,
                    on_change=lambda e: _update_strategy("scan_interval", int(e.value)),
                ).classes("w-full")

        # ==============================================================
        # RIGHT: Tabs — Live Monitoring + Backtest
        # ==============================================================
        with splitter.after:
            with ui.tabs().classes("w-full") as tabs:
                live_tab = ui.tab("Live Monitoring")
                backtest_tab = ui.tab("Backtest")
                checklist_tab = ui.tab("Checklist")
                journal_tab = ui.tab("Journal")
                kz_tab = ui.tab("Kill Zones")
                paper_tab = ui.tab("Paper Trading")

            with ui.tab_panels(tabs, value=live_tab).classes("w-full"):
                # --- Live tab ---
                with ui.tab_panel(live_tab):
                    ui.label("Biais Marche").classes("text-h6")
                    bias_row = ui.row().classes("q-mb-md gap-4 flex-wrap")

                    ui.separator()

                    ui.label("Order Blocks Actifs").classes("text-h6")
                    ob_columns = [
                        {"name": "pair", "label": "Paire", "field": "pair", "align": "left"},
                        {"name": "direction", "label": "Dir", "field": "dir"},
                        {"name": "ob_time", "label": "Cloture OB", "field": "ob_time"},
                        {"name": "top", "label": "Top", "field": "top"},
                        {"name": "bottom", "label": "Bottom", "field": "bottom"},
                        {"name": "strength", "label": "Force %", "field": "strength"},
                    ]
                    ob_table = ui.table(
                        columns=ob_columns, rows=[], row_key="pair"
                    ).classes("w-full")

                    ui.separator()

                    ui.label("Historique des Signaux").classes("text-h6")
                    sig_columns = [
                        {"name": "pair", "label": "Paire", "field": "pair", "align": "left"},
                        {"name": "dir", "label": "Dir", "field": "dir"},
                        {"name": "ob_time", "label": "Cloture OB", "field": "ob_time"},
                        {"name": "top", "label": "Top", "field": "top"},
                        {"name": "bottom", "label": "Bottom", "field": "bottom"},
                        {"name": "strength", "label": "Force %", "field": "strength"},
                        {"name": "raison", "label": "Raison force", "field": "raison"},
                    ]
                    sig_table = ui.table(
                        columns=sig_columns, rows=[], row_key="id"
                    ).classes("w-full")

                    last_scan_label = ui.label("").classes("text-caption text-grey q-mt-md")
                    error_label = ui.label("").classes("text-caption text-negative")

                # --- Backtest tab ---
                with ui.tab_panel(backtest_tab):
                    build_backtest_panel(_env, _strategy, _state)

                # --- Checklist tab ---
                with ui.tab_panel(checklist_tab):
                    build_checklist_panel(_state)

                # --- Journal tab ---
                with ui.tab_panel(journal_tab):
                    build_journal_panel(_state)

                # --- Kill Zones tab ---
                with ui.tab_panel(kz_tab):
                    build_killzone_panel(_strategy)

                # --- Paper Trading tab ---
                with ui.tab_panel(paper_tab):
                    from .paper_panel import build_paper_panel
                    _paper_refresh = build_paper_panel(_paper_portfolio)
                    # Register for periodic refresh
                    _paper_refresh_funcs.append(_paper_refresh)

    # ------------------------------------------------------------------
    # Periodic UI refresh (every 2 seconds)
    # ------------------------------------------------------------------
    async def refresh_ui():
        # Sync status pill + button state with the actual global scanner state
        if _scanner and _scanner.running:
            status_label.set_content(
                '<div class="status-pill status-running"><span class="status-dot"></span>Running</div>'
            )
            start_btn.set_enabled(False)
            stop_btn.set_enabled(True)
        else:
            status_label.set_content(
                '<div class="status-pill status-stopped"><span class="status-dot"></span>Stopped</div>'
            )
            start_btn.set_enabled(True)
            stop_btn.set_enabled(False)

        if _scanner is None:
            return

        # Bias cards
        bias_row.clear()
        with bias_row:
            for pair in _strategy.pairs:
                bias = _scanner.current_biases.get(pair, {})
                direction = bias.get("direction", 0)
                if direction == 1:
                    color, icon, text = "positive", "trending_up", "BULLISH"
                elif direction == -1:
                    color, icon, text = "negative", "trending_down", "BEARISH"
                else:
                    color, icon, text = "grey", "remove", "NEUTRE"
                with ui.card().classes("items-center q-pa-sm"):
                    ui.label(pair).classes("text-weight-bold")
                    ui.icon(icon, color=color, size="md")
                    ui.label(f"{text}").classes(f"text-{color} text-caption")
                    btype = bias.get("type", "")
                    if btype:
                        ui.label(btype).classes("text-caption text-grey")

        # Active OBs table
        ob_rows = []
        for ob in _scanner.active_obs:
            obt = ob.get("ob_time")
            ob_time_str = obt.strftime("%Y-%m-%d %H:%M") if obt and hasattr(obt, "strftime") else "-"
            ob_rows.append({
                "pair": ob["pair"],
                "dir": "BUY" if ob["direction"] == 1 else "SELL",
                "ob_time": ob_time_str,
                "top": f"{ob['top']:.5f}",
                "bottom": f"{ob['bottom']:.5f}",
                "strength": f"{ob['strength']:.1f}",
            })
        ob_table.rows = ob_rows
        ob_table.update()

        # Signal history
        history = await _state.get_signal_history(limit=20)
        sig_rows = []
        for h in history:
            strength = h.get("ob_strength") or 0
            if strength >= 70:
                raison = "Volume equilibre — forte accumulation institutionnelle"
            elif strength >= 50:
                raison = "Bon ratio buy/sell — zone d'interet institutionnel"
            elif strength >= 30:
                raison = "Ratio desequilibre — zone moderee"
            else:
                raison = "Volume unilateral — faible interet institutionnel"
            ob_t = h.get("ob_time") or ""
            sig_rows.append({
                "id": h["id"],
                "pair": h["pair"],
                "dir": "BUY" if h["direction"] == 1 else "SELL",
                "ob_time": ob_t[:16] if ob_t else "-",
                "top": h["entry"],
                "bottom": h["sl"],
                "strength": f"{strength:.1f}%",
                "raison": raison,
            })
        sig_table.rows = sig_rows
        sig_table.update()

        # Status labels
        last_scan_label.set_text(
            f"Dernier scan : {_scanner.last_scan_time}" if _scanner.last_scan_time else ""
        )
        error_label.set_text(_scanner.last_error)

        # Paper trading refresh
        for fn in _paper_refresh_funcs:
            try:
                fn()
            except Exception:
                pass

    ui.timer(2.0, refresh_ui)


# ======================================================================
# Actions
# ======================================================================

def _update_strategy(field: str, value) -> None:
    setattr(_strategy, field, value)
    _strategy.save()
    if _scanner is not None:
        _scanner.strategy = _strategy


async def _persistent_scanner_loop():
    """Server-level loop — survives page refreshes and browser disconnects."""
    logger.info("Persistent scanner loop started")
    while True:
        if _scanner and _scanner.running:
            try:
                await _scanner.scan_once()
            except Exception as exc:
                logger.exception("Scanner loop error: %s", exc)
            await asyncio.sleep(_strategy.scan_interval)
        else:
            await asyncio.sleep(2)


async def _init_scanner_task():
    """Startup handler — creates the background task and returns immediately."""
    asyncio.create_task(_persistent_scanner_loop())


# Register at server level — runs once, never cancelled by client disconnect
app.on_startup(_init_scanner_task)


def _start_scanning(status_label, start_btn, stop_btn) -> None:
    global _scanner

    _scanner = Scanner(env=_env, strategy=_strategy, state=_state)
    if not _scanner.start():
        ui.notify(_scanner.last_error, type="negative")
        return

    # The persistent loop (started on server boot) will pick up _scanner.running
    status_label.set_content(
        '<div class="status-pill status-running"><span class="status-dot"></span>Running</div>'
    )
    start_btn.set_enabled(False)
    stop_btn.set_enabled(True)
    ui.notify("Scanner started", type="positive")


def _stop_scanning(status_label, start_btn, stop_btn) -> None:
    global _scanner

    if _scanner is not None:
        _scanner.stop()
        _scanner = None

    status_label.set_content(
        '<div class="status-pill status-stopped"><span class="status-dot"></span>Stopped</div>'
    )
    start_btn.set_enabled(True)
    stop_btn.set_enabled(False)
    ui.notify("Scanner stopped", type="warning")
