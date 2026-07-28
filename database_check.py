from __future__ import annotations

import asyncio

from dotenv import load_dotenv

load_dotenv()

from config import DATABASE_URL
from storage import PostgresStore


async def main() -> None:
    store = PostgresStore(DATABASE_URL)
    await store.connect()
    try:
        await store.run_migrations()
        await store.cleanup_processed_audit_entries()
        pool = store.require_pool()
        database_name = await pool.fetchval("SELECT current_database()")
        version = await pool.fetchval("SHOW server_version")
        tables = await pool.fetch(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            ORDER BY table_name
            """
        )
        print(f"Connected to PostgreSQL database: {database_name}")
        print(f"PostgreSQL version: {version}")
        print("Tables:")
        for row in tables:
            print(f"- {row['table_name']}")
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
