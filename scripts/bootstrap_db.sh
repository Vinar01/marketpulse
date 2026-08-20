#!/usr/bin/env bash
# Create the local database and application role. Run once.
set -euo pipefail

PGBIN="${PGBIN:-/opt/homebrew/opt/postgresql@16/bin}"
export PATH="$PGBIN:$PATH"

DB_NAME="${DB_NAME:-marketpulse}"
DB_USER="${DB_USER:-marketpulse}"
DB_PASS="${DB_PASS:-marketpulse}"

echo "==> creating role '$DB_USER' and database '$DB_NAME'"

psql -d postgres -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$DB_USER') THEN
        CREATE ROLE $DB_USER LOGIN PASSWORD '$DB_PASS' CREATEROLE;
    END IF;
END
\$\$;
SQL

if ! psql -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
    createdb -O "$DB_USER" "$DB_NAME"
    echo "    database created"
else
    echo "    database already exists"
fi

# The app role owns the schema so it can create partitions at runtime.
psql -d "$DB_NAME" -v ON_ERROR_STOP=1 -c "ALTER SCHEMA public OWNER TO $DB_USER;"
psql -d "$DB_NAME" -v ON_ERROR_STOP=1 -c "GRANT ALL ON SCHEMA public TO $DB_USER;"

# Everything in this system is UTC. Pinning it at the database level means a
# developer machine in any timezone produces identical partition boundaries,
# identical date_trunc results, and identical test output.
psql -d postgres -v ON_ERROR_STOP=1 -c "ALTER DATABASE $DB_NAME SET timezone TO 'UTC';"

echo "==> done"
