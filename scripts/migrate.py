"""Apply sql/*.sql in filename order, exactly once each.

Alembic is the usual answer here, and for an ORM-shaped schema it would be.
This schema is DDL that Alembic cannot model or autogenerate -- declarative
range partitioning, plpgsql partition/retention functions, and role grants.
Hand-written, ordered, checksummed SQL is the honest tool for that job, and the
runner below is 60 lines.
"""
import asyncio
import hashlib
import pathlib
import sys

import asyncpg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.core.config import settings

SQL_DIR = pathlib.Path(__file__).resolve().parent.parent / "sql"

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    checksum   TEXT        NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _dsn() -> str:
    # asyncpg wants a plain postgres:// DSN, SQLAlchemy wants the +asyncpg driver tag.
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://")


async def main() -> int:
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(BOOTSTRAP)
        applied = {
            r["filename"]: r["checksum"]
            for r in await conn.fetch("SELECT filename, checksum FROM schema_migrations")
        }

        for path in sorted(SQL_DIR.glob("*.sql")):
            body = path.read_text()
            checksum = hashlib.sha256(body.encode()).hexdigest()[:16]

            if path.name in applied:
                if applied[path.name] != checksum:
                    print(f"  ! {path.name} changed after being applied "
                          f"({applied[path.name]} -> {checksum}). Refusing to re-run.")
                    print("    Add a new numbered file instead of editing an applied one.")
                    return 1
                print(f"  = {path.name} (already applied)")
                continue

            print(f"  + {path.name}")
            async with conn.transaction():
                await conn.execute(body)
                await conn.execute(
                    "INSERT INTO schema_migrations (filename, checksum) VALUES ($1, $2)",
                    path.name, checksum,
                )

        # Always top up partitions -- cheap, idempotent, and keeps a freshly
        # migrated database immediately writable.
        created = await conn.fetchval(
            "SELECT ensure_tick_partitions($1, 1)", settings.partition_ahead_days
        )
        print(f"  partitions ensured (created {created})")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
