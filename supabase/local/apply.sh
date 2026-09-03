#!/usr/bin/env bash
# Apply the shim + every migration to a local Postgres. Used by the test suite
# and by anyone who wants the real schema without a Supabase project.
set -euo pipefail
DB="${1:-${SUNROOM_TEST_DSN:-postgresql://postgres@127.0.0.1:5432/sunroom_test}}"
here="$(cd "$(dirname "$0")" && pwd)"

name="$(python3 - "$DB" <<'PY'
import sys, urllib.parse as u
print(u.urlparse(sys.argv[1]).path.lstrip('/') or 'postgres')
PY
)"
admin="${DB%/*}/postgres"

psql -q "$admin" -c "drop database if exists \"$name\" with (force)" >/dev/null 2>&1 || true
psql -q "$admin" -c "create database \"$name\"" >/dev/null

psql -q -v ON_ERROR_STOP=1 "$DB" -f "$here/0000_shim.sql" >/dev/null
for f in "$here"/../migrations/*.sql; do
  psql -q -v ON_ERROR_STOP=1 "$DB" -f "$f" >/dev/null
done
echo "applied: $(ls "$here"/../migrations/*.sql | wc -l) migration(s) -> $name"
