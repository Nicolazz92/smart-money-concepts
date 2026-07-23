#!/usr/bin/env bash
# Full redo v2 with RELATIVE min_risk guard (0.5% of price).
# All 10 runs: MOEX (4 WF + 2 universe) + Bybit (4 WF).
set -e
cd ~/smart-money-concepts
export PYTHONIOENCODING=utf-8

echo "=== REDO V2 START ==="
date

echo "=== [1/10] MOEX WF 1m 60M ==="
.venv/bin/python -u scripts/walk_forward.py --venue moex --oos-months 1 > /tmp/v2_moex_1m.log 2>&1
tail -3 /tmp/v2_moex_1m.log

echo "=== [2/10] MOEX WF 3m 60M ==="
.venv/bin/python -u scripts/walk_forward.py --venue moex --oos-months 3 > /tmp/v2_moex_3m.log 2>&1
tail -3 /tmp/v2_moex_3m.log

echo "=== [3/10] MOEX WF 1m 4H ==="
.venv/bin/python -u scripts/walk_forward.py --venue moex --oos-months 1 --ob-tf 4H > /tmp/v2_moex_1m_4H.log 2>&1
tail -3 /tmp/v2_moex_1m_4H.log

echo "=== [4/10] MOEX WF 3m 4H ==="
.venv/bin/python -u scripts/walk_forward.py --venue moex --oos-months 3 --ob-tf 4H > /tmp/v2_moex_3m_4H.log 2>&1
tail -3 /tmp/v2_moex_3m_4H.log

echo "=== [5/10] MOEX Universe 500 60M ==="
.venv/bin/python scripts/backtest_universe.py --list data/_moex_all_tqbr.json --venue moex --ob-tf 60M --start 2023-03-01 --end 2026-07-18 --min-rr 1.0 --cost-preset strateg --out-report data/moex_500_1D60M_report.csv --out-signals data/moex_500_1D60M_signals.csv > /tmp/v2_univ_60M.log 2>&1
tail -5 /tmp/v2_univ_60M.log

echo "=== [6/10] MOEX Universe 500 4H ==="
.venv/bin/python scripts/backtest_universe.py --list data/_moex_all_tqbr.json --venue moex --ob-tf 4H --start 2023-03-01 --end 2026-07-18 --min-rr 1.0 --cost-preset strateg --out-report data/moex_500_1D4H_report.csv --out-signals data/moex_500_1D4H_signals.csv > /tmp/v2_univ_4H.log 2>&1
tail -5 /tmp/v2_univ_4H.log

echo "=== [7/10] Bybit WF 1m 60M ==="
.venv/bin/python -u scripts/walk_forward.py --venue bybit --oos-months 1 > /tmp/v2_bybit_1m.log 2>&1
tail -3 /tmp/v2_bybit_1m.log

echo "=== [8/10] Bybit WF 3m 60M ==="
.venv/bin/python -u scripts/walk_forward.py --venue bybit --oos-months 3 > /tmp/v2_bybit_3m.log 2>&1
tail -3 /tmp/v2_bybit_3m.log

echo "=== [9/10] Bybit WF 1m 4H ==="
.venv/bin/python -u scripts/walk_forward.py --venue bybit --oos-months 1 --ob-tf 4H > /tmp/v2_bybit_1m_4H.log 2>&1
tail -3 /tmp/v2_bybit_1m_4H.log

echo "=== [10/10] Bybit WF 3m 4H ==="
.venv/bin/python -u scripts/walk_forward.py --venue bybit --oos-months 3 --ob-tf 4H > /tmp/v2_bybit_3m_4H.log 2>&1
tail -3 /tmp/v2_bybit_3m_4H.log

echo ""
echo "=== ALL REDO V2 DONE ==="
date
ls -la data/wf_*_summary.json data/moex_500_*.csv
