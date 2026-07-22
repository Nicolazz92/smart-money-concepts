#!/usr/bin/env python3
"""Breakdown analysis: per-year / per-sector / per-cap on existing universe signals.

Reads universe_signals.csv (from backtest_universe.py) and groups signals
by various dimensions. Doesn't re-run any backtest — just analytics.

Usage:
    python scripts/breakdown.py --signals data/moex_universe_signals.csv --venue moex
    python scripts/breakdown.py --signals data/bybit_universe_signals.csv --venue bybit
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd


# ─── MOEX classification ────────────────────────────────────────────────────
MOEX_SECTORS = {
    # Нефтегаз
    "GAZP": "oil_gas", "ROSN": "oil_gas", "TATN": "oil_gas", "TATNP": "oil_gas",
    "SIBN": "oil_gas", "NVTK": "oil_gas", "LKOH": "oil_gas",
    # Металлы/добыча
    "GMKN": "metals", "NLMK": "metals", "MAGN": "metals", "CHMF": "metals",
    "MTLR": "metals", "MTLRP": "metals", "RUAL": "metals",
    # Финансы/банки
    "SBER": "financials", "SBERP": "financials", "VTBR": "financials",
    "AFKS": "financials", "CBOM": "financials", "BSPB": "financials",
    "MOEX": "financials", "SFIN": "financials", "T": "financials",
    # IT/Телеком
    "YDEX": "it_telecom", "MTSS": "it_telecom", "RTKM": "it_telecom",
    "RTKMP": "it_telecom", "VKCO": "it_telecom", "OZON": "it_telecom",
    # Потребительский
    "MGNT": "consumer", "X5": "consumer", "WUSH": "consumer", "LENT": "consumer",
    "FIVE": "consumer", "POSI": "consumer", "SELG": "consumer",
    # Энергетика/утилиты
    "FEES": "utilities", "TGKA": "utilities", "HYDR": "utilities",
    "MSNG": "utilities", "IRAO": "utilities", "ENPG": "utilities",
    # Химия/материалы
    "PHOR": "materials", "SGZH": "materials", "AKRN": "materials",
    # Транспорт
    "AFLT": "transport", "TRNFP": "transport", "NMTP": "transport",
    # Прочее
    "PIKK": "other", "MDMG": "other", "HEAD": "other", "SMLT": "other",
    "IRKT": "other", "BANEP": "other", "ETLN": "other", "RENI": "other",
    "UGLD": "other", "LSNGP": "other", "SNGS": "other", "SNGSP": "other",
    "UPRO": "other", "RNFT": "other", "RASP": "other", "BELU": "other",
    "ASTR": "other", "OBLG": "other", "SPBE": "other", "ALRS": "other",
    "FLOT": "fund", "FESH": "other", "DOMRF": "other", "SVCB": "financials",
    "MBNK": "financials",
}

# Capitalization tiers (approximate, RUB bn, July 2026)
MOEX_CAP = {
    # Mega-cap (>2000 bn)
    "SBER": "mega", "GAZP": "mega", "LKOH": "mega", "GMKN": "mega",
    "ROSN": "mega", "NVTK": "mega",
    # Large (500-2000 bn)
    "YDEX": "large", "TATN": "large", "PLZL": "large", "SNGSP": "large",
    "OZON": "large", "MTSS": "large", "SBERP": "large", "MGNT": "large",
    "MOEX": "large", "NLMK": "large", "MAGN": "large", "AFKS": "large",
    "TRNFP": "large", "PHOR": "large", "POSI": "large", "CHMF": "large",
    # Mid (100-500 bn)
    "FLOT": "mid", "VKCO": "mid", "SNGS": "mid", "IRAO": "mid",
    "RUAL": "mid", "ETLN": "mid", "PIKK": "mid", "T": "mid",
    "SMLT": "mid", "X5": "mid", "CBOM": "mid", "BSPB": "mid",
    "MTLR": "mid", "VTBR": "mid", "FESH": "mid", "MSNG": "mid",
    "AFLT": "mid", "ALRS": "mid", "FEES": "mid", "HYDR": "mid",
    "MDMG": "mid", "WUSH": "mid",
    # Small (<100 bn) — defaults
}

# ─── Bybit classification (more rough) ──────────────────────────────────────
BYBIT_SECTORS = {
    "BTCUSDT": "majors", "ETHUSDT": "majors",
    "SOLUSDT": "majors_alt", "XRPUSDT": "majors_alt",
    "ADAUSDT": "majors_alt", "AVAXUSDT": "majors_alt",
    "DOGEUSDT": "meme", "1000PEPEUSDT": "meme", "1000BONKUSDT": "meme",
    "LTCUSDT": "majors_alt", "NEARUSDT": "L1", "SUIUSDT": "L1",
    "ZECUSDT": "privacy", "WLDUSDT": "AI", "ONDOUSDT": "RWA",
    "ENAUSDT": "stablecoin", "BTCPERP": "index", "HYPEUSDT": "DeFi",
    "BANKUSDT": "DeFi", "ACEUSDT": "Gaming", "INJUSDT": "L1",
}

BYBIT_CAP = {
    "BTCUSDT": "mega", "ETHUSDT": "mega",
    "SOLUSDT": "large", "XRPUSDT": "large",
    "DOGEUSDT": "large", "ADAUSDT": "large",
    "AVAXUSDT": "mid", "NEARUSDT": "mid", "LTCUSDT": "mid",
    "LINKUSDT": "mid", "INJUSDT": "mid",
    "1000PEPEUSDT": "mid", "1000BONKUSDT": "mid",
    "ZECUSDT": "small", "WLDUSDT": "small",
    "ONDOUSDT": "small", "ENAUSDT": "small",
    "SUIUSDT": "mid", "BTCPERP": "index",
    "HYPEUSDT": "mid", "BANKUSDT": "small", "ACEUSDT": "small",
}


def per_year(signals: pd.DataFrame) -> pd.DataFrame:
    df = signals.dropna(subset=["actual_rr"]).copy()
    df["time"] = pd.to_datetime(df["signal_time"], format="ISO8601", utc=True)
    df["year"] = df["time"].dt.year
    rows = []
    for year, g in df.groupby("year"):
        decided = g["actual_rr"].notna().sum()
        wins = (g["outcome"] == "WIN_TP1").sum()
        losses = (g["outcome"] == "LOSS").sum()
        wr = wins / max(wins + losses, 1) * 100
        rows.append({
            "year": year,
            "n_signals": len(g),
            "decided": int(wins + losses),
            "wins": wins, "losses": losses,
            "win_rate": round(wr, 1),
            "total_rr": round(g["actual_rr"].sum(), 2),
            "expectancy": round(g["actual_rr"].sum() / max(wins + losses, 1), 4),
        })
    return pd.DataFrame(rows)


def per_group(signals: pd.DataFrame, mapping: dict, col_name: str) -> pd.DataFrame:
    df = signals.dropna(subset=["actual_rr"]).copy()
    df[col_name] = df["ticker"].map(mapping).fillna("unclassified")
    rows = []
    for group, g in df.groupby(col_name):
        wins = (g["outcome"] == "WIN_TP1").sum()
        losses = (g["outcome"] == "LOSS").sum()
        wr = wins / max(wins + losses, 1) * 100
        rows.append({
            col_name: group,
            "n_tickers": g["ticker"].nunique(),
            "n_signals": len(g),
            "decided": int(wins + losses),
            "wins": wins, "losses": losses,
            "win_rate": round(wr, 1),
            "total_rr": round(g["actual_rr"].sum(), 2),
            "expectancy": round(g["actual_rr"].sum() / max(wins + losses, 1), 4),
            "tickers": ", ".join(sorted(g["ticker"].unique())),
        })
    return pd.DataFrame(rows).sort_values("total_rr", ascending=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", required=True)
    ap.add_argument("--venue", choices=["moex", "bybit"], required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    sig = pd.read_csv(args.signals)
    print(f"Loaded {len(sig)} signals from {args.signals}")
    print()

    # ─── Per-year
    print("=" * 80)
    print("PER-YEAR BREAKDOWN")
    print("=" * 80)
    py = per_year(sig)
    print(py.to_string(index=False))
    print()

    # ─── Per-sector
    print("=" * 80)
    print("PER-SECTOR BREAKDOWN")
    print("=" * 80)
    sectors = MOEX_SECTORS if args.venue == "moex" else BYBIT_SECTORS
    ps = per_group(sig, sectors, "sector")
    print(ps[["sector", "n_tickers", "n_signals", "decided", "wins", "losses",
             "win_rate", "total_rr", "expectancy"]].to_string(index=False))
    print()

    # ─── Per-cap
    print("=" * 80)
    print("PER-CAP BREAKDOWN")
    print("=" * 80)
    caps = MOEX_CAP if args.venue == "moex" else BYBIT_CAP
    pc = per_group(sig, caps, "cap")
    print(pc[["cap", "n_tickers", "n_signals", "decided", "wins", "losses",
             "win_rate", "total_rr", "expectancy"]].to_string(index=False))

    # ─── Save
    out = args.out or str(Path(args.signals).with_suffix("")) + "_breakdown.xlsx"
    try:
        with pd.ExcelWriter(out, engine="openpyxl") as w:
            py.to_excel(w, sheet_name="per_year", index=False)
            ps.to_excel(w, sheet_name="per_sector", index=False)
            pc.to_excel(w, sheet_name="per_cap", index=False)
        print(f"\nSaved breakdown: {out}")
    except Exception as e:
        # Fallback to CSV
        py.to_csv(out.replace(".xlsx", "_per_year.csv"), index=False)
        ps.to_csv(out.replace(".xlsx", "_per_sector.csv"), index=False)
        pc.to_csv(out.replace(".xlsx", "_per_cap.csv"), index=False)
        print(f"\nSaved CSVs (excel failed: {e})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
