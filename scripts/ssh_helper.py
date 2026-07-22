#!/usr/bin/env python3
"""SSH helper for running commands on remote server with sudo support.

Usage:
    python scripts/ssh_helper.py "command"
    python scripts/ssh_helper.py --sudo "apt install -y python3.12"
    python scripts/ssh_helper.py --put local_path remote_path
    python scripts/ssh_helper.py --get remote_path local_path
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
SUDO_PASS = os.environ.get("REMOTE_SUDO_PASS", "zCZiiszX")
KEY_PATH = str(Path.home() / ".ssh" / "id_ed25519")


def connect():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=HOST, port=PORT, username=USER,
        key_filename=KEY_PATH, look_for_keys=True,
        timeout=15,
    )
    return client


def run(cmd: str, sudo: bool = False, timeout: int = 300) -> int:
    client = connect()
    try:
        if sudo:
            # Pipe password to sudo via stdin
            full = f"echo '{SUDO_PASS}' | sudo -S bash -c {repr(cmd)}"
        else:
            full = cmd
        stdin, stdout, stderr = client.exec_command(full, timeout=timeout, get_pty=False)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        rc = stdout.channel.recv_exit_status()
        if out:
            sys.stdout.write(out)
        # Filter sudo password warning noise
        if err and "[sudo]" not in err and "password for" not in err.lower():
            sys.stderr.write(err)
        return rc
    finally:
        client.close()


def put(local: str, remote: str):
    client = connect()
    try:
        sftp = client.open_sftp()
        sftp.put(local, remote)
        sftp.close()
        print(f"PUT {local} → {remote} OK")
    finally:
        client.close()


def get(remote: str, local: str):
    client = connect()
    try:
        sftp = client.open_sftp()
        sftp.get(remote, local)
        sftp.close()
        print(f"GET {remote} → {local} OK")
    finally:
        client.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", help="Command to run")
    ap.add_argument("--sudo", action="store_true")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--put", nargs=2, metavar=("LOCAL", "REMOTE"))
    ap.add_argument("--get", nargs=2, metavar=("REMOTE", "LOCAL"))
    args = ap.parse_args()

    if args.put:
        put(args.put[0], args.put[1])
    elif args.get:
        get(args.get[0], args.get[1])
    elif args.cmd:
        rc = run(args.cmd, sudo=args.sudo, timeout=args.timeout)
        sys.exit(rc)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
