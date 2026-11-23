#!/bin/sh
set -eu
umask 077

data=/var/lib/kaiwu-user-archives/postgres
bin=/opt/kaiwu-user-archives/runtime/usr/lib/postgresql/17/bin
if [ -f "$data/PG_VERSION" ]; then
    test "$(cat "$data/PG_VERSION")" = 17
    exit
fi
test ! -e "$data"
stage=$(mktemp -d /var/lib/kaiwu-user-archives/init.XXXXXXXX)
"$bin/initdb" --pgdata="$stage" --username=kaiwu-user-archives \
    --encoding=UTF8 --locale=C.UTF-8 --auth-local=peer --auth-host=reject
cat > "$stage/pg_hba.conf" <<'HBA'
local  all  kaiwu-user-archives  peer map=operator
local  all  all                  reject
HBA
cat > "$stage/pg_ident.conf" <<'IDENT'
operator  root                 kaiwu-user-archives
operator  kaiwu-user-archives  kaiwu-user-archives
IDENT
mv -T "$stage" "$data"
