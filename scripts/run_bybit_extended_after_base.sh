#!/usr/bin/env bash
# Wait for base Bybit redo to finish, then run extended Bybit WF (27 combos).
# Independent of SSH session.
set -e
cd ~/smart-money-concepts
export PYTHONIOENCODING=utf-8

echo "=== Waiting for base Bybit redo (4 procs) to finish ==="
date
while true; do
    # Base redo done when wf_bybit_oos3m_4H_summary.json exists (last of 4)
    if [ -f data/wf_bybit_oos3m_4H_summary.json ]; then
        n_procs=$(ps -ef | grep '[p]ython.*walk_forward' | wc -l)
        if [ "$n_procs" -eq "0" ]; then
            echo "Base Bybit redo finished."
            break
        fi
    fi
    sleep 60
done

echo ""
echo "=== Starting EXTENDED Bybit WF (27 combos each) ==="
date

echo "--- [1/4] Bybit EXT WF OOS=1m 1D+60M ---"
.venv/bin/python -u scripts/walk_forward.py --venue bybit --oos-months 1 --extended \
    > /tmp/ext_bybit_wf_1m.log 2>&1
tail -5 /tmp/ext_bybit_wf_1m.log

echo "--- [2/4] Bybit EXT WF OOS=3m 1D+60M ---"
.venv/bin/python -u scripts/walk_forward.py --venue bybit --oos-months 3 --extended \
    > /tmp/ext_bybit_wf_3m.log 2>&1
tail -5 /tmp/ext_bybit_wf_3m.log

echo "--- [3/4] Bybit EXT WF OOS=1m 1D+4H ---"
.venv/bin/python -u scripts/walk_forward.py --venue bybit --oos-months 1 --ob-tf 4H --extended \
    > /tmp/ext_bybit_wf_1m_4H.log 2>&1
tail -5 /tmp/ext_bybit_wf_1m_4H.log

echo "--- [4/4] Bybit EXT WF OOS=3m 1D+4H ---"
.venv/bin/python -u scripts/walk_forward.py --venue bybit --oos-months 3 --ob-tf 4H --extended \
    > /tmp/ext_bybit_wf_3m_4H.log 2>&1
tail -5 /tmp/ext_bybit_wf_3m_4H.log

echo ""
echo "=== ALL EXTENDED BYBIT WF DONE ==="
date
ls -la data/wf_bybit_*_ext_summary.json
echo "Pipeline fully complete."
