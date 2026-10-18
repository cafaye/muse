"""Apply muse's SQL migrations to the database named by MUSE_DATABASE_URL."""

from __future__ import annotations

import asyncio
import glob
import os
import sys

from psycopg import AsyncConnection


async def main() -> int:
    url = os.environ.get("MUSE_DATABASE_URL")
    if not url:
        print("MUSE_DATABASE_URL is required", file=sys.stderr)
        return 2
    files = sorted(glob.glob("/app/migrations/*.sql"))
    async with await AsyncConnection.connect(url) as conn:
        async with conn.transaction():
            for f in files:
                with open(f) as fh:
                    sql = fh.read()
                await conn.execute(sql)
                print(f"applied {f}")
        await conn.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
