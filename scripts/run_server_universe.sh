#!/usr/bin/env bash
# Server-side script: after 500 tickers downloaded, resample 4H + run universe backtest on both TFs.
# Run on server (16 cores, 62GB RAM).
set -e
cd ~/smart-money-concepts
export PYTHONIOENCODING=utf-8

echo "=== [1/4] Resample 4H for all tickers with 60M ==="
.venv/bin/python scripts/resample_to_4h.py --venue moex --data-dir data 2>&1 | tail -5

echo ""
echo "=== [2/4] Universe backtest 1D+4H (all 500 tickers) ==="
date
.venv/bin/python scripts/backtest_universe.py \
    --list data/_moex_all_tqbr.json \
    --venue moex \
    --ob-tf 4H \
    --start 2023-03-01 \
    --end 2026-07-18 \
    --min-rr 1.0 \
    --cost-preset strateg \
    --out-report data/moex_500_1D4H_report.csv \
    --out-signals data/moex_500_1D4H_signals.csv \
    > /tmp/bt_500_4h.log 2>&1
echo "1D+4H done:"
tail -10 /tmp/bt_500_4h.log

echo ""
echo "=== [3/4] Universe backtest 1D+60M (all 500 tickers) ==="
date
.venv/bin/python scripts/backtest_universe.py \
    --list data/_moex_all_tqbr.json \
    --venue moex \
    --ob-tf 60M \
    --start 2023-03-01 \
    --end 2026-07-18 \
    --min-rr 1.0 \
    --cost-preset strateg \
    --out-report data/moex_500_1D60M_report.csv \
    --out-signals data/moex_500_1D60M_signals.csv \
    > /tmp/bt_500_60m.log 2>&1
echo "1D+60M done:"
tail -10 /tmp/bt_500_60m.log

echo ""
echo "=== [4/4] DONE ==="
date
echo "Files:"
ls -la data/moex_500_*.csv
