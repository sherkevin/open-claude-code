#!/bin/sh
set -eu
umask 077

root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root"
KAIWU_TEST_POSTGRES_BIN=${1:-$(pg_config --bindir)}
export KAIWU_TEST_POSTGRES_BIN
bin=$KAIWU_TEST_POSTGRES_BIN
mkdir -p .venv
testdir=$(mktemp -d "$root/.venv/kaiwu-storage-test.XXXXXXXX")
data="$testdir/data"
PGHOST=$(mktemp -d /tmp/kaiwu-pg.XXXXXXXX)
PGPORT=55444
PGDATABASE=postgres
PGUSER=$(id -un)
export PGHOST PGPORT PGDATABASE PGUSER
unset PGHOSTADDR PGSERVICE PGOPTIONS
stop_postgres() {
    if [ -f "$data/postmaster.pid" ]; then
        "$bin/pg_ctl" -D "$data" -m fast -w stop >/dev/null
    fi
    rmdir "$PGHOST"
}
trap stop_postgres EXIT

# The temporary socket directory is accessible only to the test runner.
# Trust authentication here lets the test check database grants using
# several SQL roles. The deployment unit uses peer authentication.
"$bin/initdb" -D "$data" --auth-local=trust --auth-host=reject --encoding=UTF8 --no-locale > "$testdir/init.log"
"$bin/pg_ctl" -D "$data" -l "$testdir/server.log" \
    -o "-k '$PGHOST' -p $PGPORT -c listen_addresses='' -c max_connections=32" -w start
PYTHONPATH="server${PYTHONPATH:+:$PYTHONPATH}" python3 -m pytest -q -s -c server/pyproject.toml \
    server/tests/test_kaiwu_user_storage.py
