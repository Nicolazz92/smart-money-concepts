#!/usr/bin/env bash
# Full re-run of all MOEX tests with the R:R cap fix applied.
# Run on server (16 cores, 62GB RAM).
set -e
cd ~/smart-money-concepts
export PYTHONIOENCODING=utf-8

# Clear old MOEX WF artefacts to avoid checkpoint reuse
rm -f data/wf_moex_*_summary.json data/wf_moex_*_signals.csv data/wf_moex_*_params.csv data/.wf_moex_*_ckpt.json
mkdir -p data/redo_backup
mv -t data/redo_backup/ data/moex_500_1D60M_*.csv data/moex_500_1D4H_*.csv 2>/dev/null || true

echo "=== [1/6] MOEX WF OOS=1m 1D+60M ==="
date
.venv/bin/python -u scripts/walk_forward.py --venue moex --oos-months 1 \
    > /tmp/redo_moex_wf_1m.log 2>&1
echo "1m done. Tail:"
tail -5 /tmp/redo_moex_wf_1m.log

echo ""
echo "=== [2/6] MOEX WF OOS=3m 1D+60M ==="
date
.venv/bin/python -u scripts/walk_forward.py --venue moex --oos-months 3 \
    > /tmp/redo_moex_wf_3m.log 2>&1
echo "3m done. Tail:"
tail -5 /tmp/redo_moex_wf_3m.log

echo ""
echo "=== [3/6] MOEX WF OOS=1m 1D+4H ==="
date
.venv/bin/python -u scripts/walk_forward.py --venue moex --oos-months 1 --ob-tf 4H \
    > /tmp/redo_moex_wf_1m_4H.log 2>&1
echo "1m 4H done. Tail:"
tail -5 /tmp/redo_moex_wf_1m_4H.log

echo ""
echo "=== [4/6] MOEX WF OOS=3m 1D+4H ==="
date
.venv/bin/python -u scripts/walk_forward.py --venue moex --oos-months 3 --ob-tf 4H \
    > /tmp/redo_moex_wf_3m_4H.log 2>&1
echo "3m 4H done. Tail:"
tail -5 /tmp/redo_moex_wf_3m_4H.log

echo ""
echo "=== [5/6] MOEX Universe 500 1D+60M ==="
date
.venv/bin/python scripts/backtest_universe.py \
    --list data/_moex_all_tqbr.json --venue moex --ob-tf 60M \
    --start 2023-03-01 --end 2026-07-18 --min-rr 1.0 --cost-preset strateg \
    --out-report data/moex_500_1D60M_report.csv \
    --out-signals data/moex_500_1D60M_signals.csv \
    > /tmp/redo_univ_60M.log 2>&1
echo "60M universe done. Tail:"
tail -8 /tmp/redo_univ_60M.log

echo ""
echo "=== [6/6] MOEX Universe 500 1D+4H ==="
date
.venv/bin/python scripts/backtest_universe.py \
    --list data/_moex_all_tqbr.json --venue moex --ob-tf 4H \
    --start 2023-03-01 --end 2026-07-18 --min-rr 1.0 --cost-preset strateg \
    --out-report data/moex_500_1D4H_report.csv \
    --out-signals data/moex_500_1D4H_signals.csv \
    > /tmp/redo_univ_4H.log 2>&1
echo "4H universe done. Tail:"
tail -8 /tmp/redo_univ_4H.log

echo ""
echo "=== ALL DONE ==="
date
ls -la data/wf_moex_*_summary.json data/moex_500_*.csv
