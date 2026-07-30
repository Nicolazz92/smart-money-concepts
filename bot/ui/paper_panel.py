"""NiceGUI panel for Paper Trading tab.

Shows: equity, open positions, closed positions, reset button.
"""

from __future__ import annotations

import logging

from nicegui import ui

from ..paper.portfolio import Portfolio

logger = logging.getLogger(__name__)


def build_paper_panel(portfolio: Portfolio):
    """Build the Paper Trading tab UI.

    Parameters
    ----------
    portfolio : Portfolio
        The paper portfolio to display and manage.
    """

    # ─── Equity summary ───
    ui.label("📊 Paper Trading").classes("text-h5 q-mb-md")

    equity_card = ui.card().classes("q-mb-md w-full")
    with equity_card:
        ui.row().classes("w-full items-center justify-between").bind(
            lambda: _stats_text(portfolio)
        )

    # ─── Open positions ───
    ui.label("Open Positions").classes("text-h6 q-mt-md q-mb-sm")
    open_table = ui.table(
        columns=[
            {"name": "ticker", "label": "Ticker", "field": "ticker", "align": "left"},
            {"name": "dir", "label": "Direction", "field": "direction", "align": "left"},
            {"name": "entry", "label": "Entry", "field": "entry", "align": "right"},
            {"name": "sl", "label": "SL", "field": "sl", "align": "right"},
            {"name": "tp1", "label": "TP1", "field": "tp1", "align": "right"},
            {"name": "rr", "label": "R:R", "field": "rr1", "align": "right"},
            {"name": "size", "label": "Size %", "field": "position_size_pct", "align": "right"},
            {"name": "opened", "label": "Opened", "field": "open_time", "align": "left"},
        ],
        rows=[],
        row_key="id",
    ).classes("w-full q-mb-md")

    # ─── Closed positions ───
    ui.label("Closed Positions").classes("text-h6 q-mt-md q-mb-sm")
    closed_table = ui.table(
        columns=[
            {"name": "ticker", "label": "Ticker", "field": "ticker", "align": "left"},
            {"name": "dir", "label": "Direction", "field": "direction", "align": "left"},
            {"name": "entry", "label": "Entry", "field": "entry", "align": "right"},
            {"name": "exit", "label": "Exit", "field": "exit_price", "align": "right"},
            {"name": "outcome", "label": "Outcome", "field": "status", "align": "left"},
            {"name": "pnl", "label": "P/L %", "field": "pnl_pct", "align": "right"},
        ],
        rows=[],
        row_key="id",
    ).classes("w-full q-mb-md")

    # ─── Reset button ───
    def do_reset():
        portfolio.reset()
        _refresh(open_table, closed_table, portfolio)
        ui.notify("Paper account reset", type="positive")

    ui.button("Reset Paper Account", on_click=do_reset, color="negative").classes("q-mt-md")

    # ─── Refresh function ───
    def refresh_paper():
        _refresh(open_table, closed_table, portfolio)

    return refresh_paper


def _stats_text(portfolio: Portfolio) -> str:
    s = portfolio.stats()
    color = "green" if s["return_pct"] >= 0 else "red"
    return (
        f"Equity: {s['equity']:,.0f} RUB  |  "
        f"Return: {s['return_pct']:+.1f}%  |  "
        f"Open: {s['open_positions']}  |  "
        f"Closed: W{s['wins']}/L{s['losses']} (WR {s['win_rate']:.0f}%)"
    )


def _refresh(open_table, closed_table, portfolio: Portfolio):
    """Refresh the tables with current portfolio data."""
    open_rows = [
        {
            "id": p.id or id(p),
            "ticker": p.ticker,
            "direction": p.direction,
            "entry": f"{p.entry:,.2f}",
            "sl": f"{p.sl:,.2f}",
            "tp1": f"{p.tp1:,.2f}",
            "rr1": f"{p.rr1:.1f}",
            "position_size_pct": f"{p.position_size_pct:.0f}%",
            "open_time": p.open_time[:19] if p.open_time else "",
        }
        for p in portfolio.open_positions
    ]
    open_table.update_rows(open_rows)

    closed_rows = [
        {
            "id": p.id or hash(p),
            "ticker": p.ticker,
            "direction": p.direction,
            "entry": f"{p.entry:,.2f}",
            "exit_price": f"{p.exit_price:,.2f}",
            "status": p.status,
            "pnl_pct": f"{p.pnl_pct:+.2f}%",
        }
        for p in reversed(portfolio.closed_positions[-50:])  # last 50
    ]
    closed_table.update_rows(closed_rows)
