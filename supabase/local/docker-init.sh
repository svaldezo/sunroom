#!/bin/sh
# Applies the real migrations when the compose database first starts, so the
# local stack and Supabase run the same SQL rather than a hand-kept copy.
set -e
for f in /docker-entrypoint-initdb.d/migrations/*.sql; do
  echo "applying $(basename "$f")"
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f "$f"
done
