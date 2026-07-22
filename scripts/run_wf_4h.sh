#!/usr/bin/env bash
# Run all 4 walk-forward configurations on 1D+4H timeframe.
set -e
cd "C:/Users/Nicolazz/ZCodeProject/smart-money-concepts"

# Use parallel joblib workers (proven stable on this setup)
unset WF_SEQUENTIAL
export PYTHONIOENCODING=utf-8

echo "=== [1/4] Bybit 1D+4H OOS=1m ==="
.venv/Scripts/python.exe -u scripts/walk_forward.py --venue bybit --oos-months 1 --ob-tf 4H \
    > data/wf_bybit_oos1m_4H.log 2>&1 || echo "FAILED bybit 1m 4H (exit $?)"

echo "=== [2/4] Bybit 1D+4H OOS=3m ==="
.venv/Scripts/python.exe -u scripts/walk_forward.py --venue bybit --oos-months 3 --ob-tf 4H \
    > data/wf_bybit_oos3m_4H.log 2>&1 || echo "FAILED bybit 3m 4H (exit $?)"

echo "=== [3/4] MOEX 1D+4H OOS=1m ==="
.venv/Scripts/python.exe -u scripts/walk_forward.py --venue moex --oos-months 1 --ob-tf 4H \
    > data/wf_moex_oos1m_4H.log 2>&1 || echo "FAILED moex 1m 4H (exit $?)"

echo "=== [4/4] MOEX 1D+4H OOS=3m ==="
.venv/Scripts/python.exe -u scripts/walk_forward.py --venue moex --oos-months 3 --ob-tf 4H \
    > data/wf_moex_oos3m_4H.log 2>&1 || echo "FAILED moex 3m 4H (exit $?)"

echo "=== ALL 1D+4H DONE ==="
date
