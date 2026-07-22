#!/usr/bin/env bash
# Wait for MOEX redo to finish, then run Bybit WF redo automatically.
# Independent of SSH session — runs on server via nohup+disown.
set -e
cd ~/smart-money-concepts
export PYTHONIOENCODING=utf-8

echo "=== Waiting for MOEX redo to finish (checking every 60s) ==="
date
while true; do
    # MOEX redo is done when both universe files exist (last 2 of 6 steps)
    if [ -f data/moex_500_1D60M_report.csv ] && [ -f data/moex_500_1D4H_report.csv ]; then
        # Extra check: no python procs running
        n_procs=$(ps -ef | grep '[p]ython.*backtest_universe\|[p]ython.*walk_forward' | wc -l)
        if [ "$n_procs" -eq "0" ]; then
            echo "MOEX redo finished. Both universe files exist and no procs running."
            break
        fi
    fi
    sleep 60
done

echo ""
echo "=== MOEX REDO COMPLETE ==="
date
ls -la data/wf_moex_*_summary.json data/moex_500_*.csv
echo ""

# Clear old Bybit WF artefacts (force fresh start)
rm -f data/wf_bybit_*_summary.json data/wf_bybit_*_signals.csv data/wf_bybit_*_params.csv
rm -f data/.wf_bybit_*_ckpt.json

echo "=== [1/4] Bybit WF OOS=1m 1D+60M ==="
date
.venv/bin/python -u scripts/walk_forward.py --venue bybit --oos-months 1 \
    > /tmp/redo_bybit_wf_1m.log 2>&1
echo "Bybit 1m done:"
tail -5 /tmp/redo_bybit_wf_1m.log

echo ""
echo "=== [2/4] Bybit WF OOS=3m 1D+60M ==="
date
.venv/bin/python -u scripts/walk_forward.py --venue bybit --oos-months 3 \
    > /tmp/redo_bybit_wf_3m.log 2>&1
echo "Bybit 3m done:"
tail -5 /tmp/redo_bybit_wf_3m.log

echo ""
echo "=== [3/4] Bybit WF OOS=1m 1D+4H ==="
date
.venv/bin/python -u scripts/walk_forward.py --venue bybit --oos-months 1 --ob-tf 4H \
    > /tmp/redo_bybit_wf_1m_4H.log 2>&1
echo "Bybit 1m 4H done:"
tail -5 /tmp/redo_bybit_wf_1m_4H.log

echo ""
echo "=== [4/4] Bybit WF OOS=3m 1D+4H ==="
date
.venv/bin/python -u scripts/walk_forward.py --venue bybit --oos-months 3 --ob-tf 4H \
    > /tmp/redo_bybit_wf_3m_4H.log 2>&1
echo "Bybit 3m 4H done:"
tail -5 /tmp/redo_bybit_wf_3m_4H.log

echo ""
echo "=== ALL BYBIT REDO DONE ==="
date
ls -la data/wf_bybit_*_summary.json

# Optional: send webhook/email notification here when implemented
echo "Pipeline complete. Ready to download results."
