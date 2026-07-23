#!/usr/bin/env python3
"""Download ALL data files from server to local (paramiko sftp).

Designed to run periodically — only downloads new/changed files by size.

Usage:
    python scripts/sync_server_data.py              # sync everything
    python scripts/sync_server_data.py --results    # only WF/universe results (small)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import paramiko

HOST = "195.208.16.1"
PORT = 40141
USER = "user"
KEY_PATH = str(Path.home() / ".ssh" / "id_ed25519")
REMOTE_DATA = "/home/user/smart-money-concepts/data"
LOCAL_DATA = Path(__file__).resolve().parent.parent / "data"

# Patterns for "results only" mode (small JSON/CSV from WF + universe)
RESULTS_PATTERNS = [
    "wf_",           # all WF outputs (summary, signals, params)
    "moex_500_",     # universe results on 500 tickers
    "bybit_1D4H_",   # bybit universe 4H
    "_moex_all_tqbr.json",
    "_bybit_final.json",
]


def connect():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=HOST, port=PORT, username=USER,
                   key_filename=KEY_PATH, look_for_keys=True, timeout=15)
    return client


def sync(results_only: bool = False):
    LOCAL_DATA.mkdir(parents=True, exist_ok=True)
    client = connect()
    try:
        sftp = client.open_sftp()
        remote_files = sftp.listdir(REMOTE_DATA)
        downloaded = 0
        skipped = 0
        errors = 0

        for fname in remote_files:
            if results_only:
                if not any(p in fname for p in RESULTS_PATTERNS):
                    continue

            remote_path = f"{REMOTE_DATA}/{fname}"
            local_path = LOCAL_DATA / fname

            try:
                remote_stat = sftp.stat(remote_path)
                remote_size = remote_stat.st_size

                # Skip if local file exists with same size
                if local_path.exists():
                    local_size = local_path.stat().st_size
                    if local_size == remote_size:
                        skipped += 1
                        continue

                sftp.get(remote_path, str(local_path))
                downloaded += 1
                if downloaded <= 20 or downloaded % 50 == 0:
                    print(f"  [{downloaded}] {fname} ({remote_size:,} bytes)")
            except Exception as e:
                errors += 1
                if errors <= 5:
                    print(f"  ERROR {fname}: {e}")

        sftp.close()
        print(f"\nSync complete: {downloaded} downloaded, {skipped} skipped, {errors} errors")
        return downloaded
    finally:
        client.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", action="store_true",
                    help="Only download WF/universe results (skip raw CSV data)")
    args = ap.parse_args()
    sync(results_only=args.results)


if __name__ == "__main__":
    main()
