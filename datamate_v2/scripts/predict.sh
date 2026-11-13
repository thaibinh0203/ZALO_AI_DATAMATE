#!/usr/bin/env bash
set -euo pipefail
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1
export TOKENIZERS_PARALLELISM=false
cd /code
PG_BIN="$(pg_config --bindir)"
PG_WORK="$(mktemp -d /tmp/datamate-postgres.XXXXXX)"
mkdir -p "$PG_WORK/socket" /result/logs
cleanup() { "$PG_BIN/pg_ctl" -D "$PG_WORK/data" -m fast -w stop >/dev/null 2>&1 || true; }
trap cleanup EXIT
"$PG_BIN/initdb" -D "$PG_WORK/data" -U datamate_owner --encoding=UTF8 --no-locale --auth-local=trust --auth-host=reject >"$PG_WORK/init.log"
"$PG_BIN/pg_ctl" -D "$PG_WORK/data" -l "$PG_WORK/server.log" -o "-h '' -k $PG_WORK/socket -c unix_socket_permissions=0700" -w start
export DATAMATE_ADMIN_DSN="host=$PG_WORK/socket dbname=postgres user=datamate_owner"
export DATAMATE_READONLY_DSN="host=$PG_WORK/socket dbname=postgres user=datamate_reader"
python /code/datamate_v2/scripts/setup_database.py --no-password
# /result contains real incremental files, so answers survive an interrupted container.
python -m datamate_v2.cli run --split /data --output /result --submission-mode --cache /tmp/datamate-cache --model "${DATAMATE_MODEL:-qwen3_4b}"
