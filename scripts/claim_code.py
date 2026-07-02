#!/usr/bin/env python3
"""
Generate or display the one-time NNM root claim code.

Intended for operators with host/container access. The code is written
to state/admin_bootstrap.txt and must be pasted into /enable-multiuser
to claim the instance. If the instance is already claimed, no code is
issued.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate or display the NNM root claim code.",
    )
    parser.add_argument("--host", default="localhost", help="Host shown in the claim URL.")
    parser.add_argument("--port", default="9500", help="HTTP port shown in the claim URL.")
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="Replace any existing unclaimed code before printing.",
    )
    args = parser.parse_args()

    from lib import admin_bootstrap, auth_db, db

    try:
        if await auth_db.count_admins() > 0:
            admin_bootstrap.delete_bootstrap_file()
            print("Instance already claimed; no claim code was generated.")
            return 0

        if args.rotate:
            admin_bootstrap.delete_bootstrap_file()

        path = await admin_bootstrap.ensure_bootstrap_if_needed()
        token = admin_bootstrap.read_bootstrap_token()
        if not path or not token:
            print("No claim code was generated. Check server logs.", file=sys.stderr)
            return 1

        print("NNM root claim is available.")
        print(f"Claim URL:  http://{args.host}:{args.port}/enable-multiuser")
        print(f"Claim code: {token}")
        print("")
        print("Claiming is optional. Skip it to keep legacy single-user behavior.")
        print("Use --rotate if this code may have been exposed before claim.")
        return 0
    finally:
        await db.close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
