#!/usr/bin/env bash
# Run all 4 walk-forward configurations sequentially.
# Designed to run in background overnight — resilient to single-run crashes.
set -e
cd "C:/Users/Nicolazz/ZCodeProject/smart-money-concepts"

# Use parallel joblib workers (default — remove WF_SEQUENTIAL)
unset WF_SEQUENTIAL
export PYTHONIOENCODING=utf-8

echo "=== [1/4] Bybit OOS=1m ==="
.venv/Scripts/python.exe -u scripts/walk_forward.py --venue bybit --oos-months 1 \
    > data/wf_bybit_1m.log 2>&1 || echo "FAILED bybit 1m (exit $?)"

echo "=== [2/4] Bybit OOS=3m ==="
.venv/Scripts/python.exe -u scripts/walk_forward.py --venue bybit --oos-months 3 \
    > data/wf_bybit_3m.log 2>&1 || echo "FAILED bybit 3m (exit $?)"

echo "=== [3/4] MOEX OOS=1m ==="
.venv/Scripts/python.exe -u scripts/walk_forward.py --venue moex --oos-months 1 \
    > data/wf_moex_1m.log 2>&1 || echo "FAILED moex 1m (exit $?)"

echo "=== [4/4] MOEX OOS=3m ==="
.venv/Scripts/python.exe -u scripts/walk_forward.py --venue moex --oos-months 3 \
    > data/wf_moex_3m.log 2>&1 || echo "FAILED moex 3m (exit $?)"

echo "=== ALL DONE ==="
date
