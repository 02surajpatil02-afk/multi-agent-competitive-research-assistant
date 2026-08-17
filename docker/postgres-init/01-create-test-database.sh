#!/bin/sh
# WHY THIS FILE EXISTS
#     The real-PostgreSQL integration suite drops and recreates its schema between test cases.
#     Pointing it at the same database `uvicorn app:app` uses would mean a test run silently
#     destroys local development data, so it gets a database of its own and `pgharness.py`
#     refuses any URL whose database name does not say `test`.
#
#     Run by the postgres image once, when the data volume is empty. `docker compose down -v`
#     is what makes it run again.
set -eu

TEST_DB="${POSTGRES_TEST_DB:-research_test}"

# `CREATE DATABASE` has no IF NOT EXISTS, so existence is checked first. The init directory
# only runs on an empty volume, but a script that is safe to run twice is one less thing to
# reason about when it is invoked by hand.
if [ -z "$(psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
    "SELECT 1 FROM pg_database WHERE datname = '${TEST_DB}'")" ]; then
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE DATABASE \"${TEST_DB}\""
    echo "created database ${TEST_DB}"
else
    echo "database ${TEST_DB} already exists"
fi
