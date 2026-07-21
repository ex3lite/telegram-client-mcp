#!/usr/bin/env bash
set -Eeuo pipefail

umask 027

SOURCE_DIR=${DCA_SOURCE_DIR:-/opt/dca/source}
APP_SUBDIR=ai-frontend-communication
RELEASES_DIR=/opt/dca/releases
CURRENT_LINK=/opt/dca/current
PREVIOUS_LINK=/opt/dca/previous
STATE_DIR=/var/lib/dca
BACKUP_DIR=/var/backups/dca
ENV_FILE=/etc/dca/dca.env
LOCK_FILE=/run/lock/dca-deploy.lock
SERVICE_USER=dca
SERVICES=(dca-api.service dca-worker.service)
SMOKE_URL='http://172.18.0.1:8000/health/ready?deep=true'

SERVICES_STOPPED=0
MIGRATION_ATTEMPTED=0
RECOVERY_RELEASE=
RECOVERY_REVISION=
RECOVERY_COMMAND=
RECOVERY_CURRENT=
LAST_BACKUP=

die() {
  echo "dca-deploy: $*" >&2
  exit 1
}

require_root() {
  [[ ${EUID} -eq 0 ]] || die "run as root"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "missing command: $1"
}

acquire_lock() {
  exec 9>"$LOCK_FILE"
  flock -n 9 || die "another deployment is running"
}

atomic_link() {
  local link=$1 target=$2
  local temporary="${link}.new.$$"
  ln -s "$target" "$temporary"
  mv -Tf "$temporary" "$link"
}

current_target() {
  if [[ -L $1 ]]; then
    readlink -f "$1"
  fi
}

run_with_env() {
  local release=$1
  shift
  [[ -r $ENV_FILE ]] || die "missing $ENV_FILE"
  (
    cd "$release"
    "$release/.venv/bin/python" - "$ENV_FILE" "$@" <<'PY'
import os
import re
import shlex
import sys

path, *command = sys.argv[1:]
environment = os.environ.copy()
with open(path, encoding="utf-8") as source:
    for number, raw_line in enumerate(source, 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SystemExit(f"{path}:{number}: expected KEY=VALUE")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise SystemExit(f"{path}:{number}: invalid environment name")
        values = shlex.split(raw_value, comments=False, posix=True)
        if len(values) > 1:
            raise SystemExit(f"{path}:{number}: quote values containing spaces")
        environment[key] = values[0] if values else ""
if not command:
    raise SystemExit("missing command")
os.execvpe(command[0], command, environment)
PY
  )
}

validate_sql_pairs() {
  local release=$1 migration up down
  local directory="$release/migrations/sql"
  [[ -d $directory ]] || return 0

  for up in "$directory"/*.up.sql; do
    [[ -e $up ]] || continue
    down=${up%.up.sql}.down.sql
    [[ -f $down ]] || die "missing SQL down migration for $(basename "$up")"
  done
  for down in "$directory"/*.down.sql; do
    [[ -e $down ]] || continue
    up=${down%.down.sql}.up.sql
    [[ -f $up ]] || die "missing SQL up migration for $(basename "$down")"
  done
  while IFS= read -r migration; do
    [[ $(basename "$migration") =~ ^[0-9a-f]+(_[a-z0-9_-]+)?\.(up|down)\.sql$ ]] ||
      die "invalid SQL migration name: $(basename "$migration")"
  done < <(find "$directory" -maxdepth 1 -type f \( -name '*.up.sql' -o -name '*.down.sql' \) -print)
}

alembic_head() {
  local release=$1 output count
  output=$(run_with_env "$release" "$release/.venv/bin/alembic" heads)
  count=$(awk 'NF { count++ } END { print count + 0 }' <<<"$output")
  [[ $count -eq 1 ]] || die "release must have exactly one Alembic head (found $count)"
  awk 'NF { print $1; exit }' <<<"$output"
}

alembic_current() {
  local release=$1 output
  output=$(run_with_env "$release" "$release/.venv/bin/alembic" current)
  awk 'NF { print $1; exit }' <<<"$output"
}

prepare_release() {
  local status revision release uv_bin

  [[ -d $SOURCE_DIR/.git ]] || die "missing git checkout: $SOURCE_DIR"
  status=$(git -C "$SOURCE_DIR" status --porcelain --untracked-files=normal)
  [[ -z $status ]] || die "source checkout is dirty: $SOURCE_DIR"
  git -C "$SOURCE_DIR" checkout main >&2
  git -C "$SOURCE_DIR" pull --ff-only origin main >&2
  status=$(git -C "$SOURCE_DIR" status --porcelain --untracked-files=normal)
  [[ -z $status ]] || die "source checkout became dirty after pull"

  revision=$(git -C "$SOURCE_DIR" rev-parse HEAD)
  release="$RELEASES_DIR/$(date -u +%Y%m%dT%H%M%SZ)-${revision:0:12}"
  [[ ! -e $release ]] || die "release already exists: $release"
  mkdir -p "$release"
  git -C "$SOURCE_DIR" archive "$revision" "$APP_SUBDIR" |
    tar -x -C "$release" --strip-components=1

  validate_sql_pairs "$release"
  chown -R "$SERVICE_USER:$SERVICE_USER" "$release"
  uv_bin=$(command -v uv)
  runuser -u "$SERVICE_USER" -- env \
    HOME="$STATE_DIR" UV_CACHE_DIR="$STATE_DIR/.cache/uv" \
    "$uv_bin" --directory "$release" sync \
    --frozen --no-dev --no-editable --python 3.14 >&2
  runuser -u "$SERVICE_USER" -- env \
    HOME="$STATE_DIR" XDG_CACHE_HOME="$STATE_DIR/.cache" \
    XDG_DATA_HOME="$STATE_DIR/.local/share" \
    corepack pnpm --dir "$release/web" install --frozen-lockfile >&2
  runuser -u "$SERVICE_USER" -- env \
    HOME="$STATE_DIR" XDG_CACHE_HOME="$STATE_DIR/.cache" \
    XDG_DATA_HOME="$STATE_DIR/.local/share" \
    corepack pnpm --dir "$release/web" build >&2

  run_with_env "$release" "$release/.venv/bin/python" -c \
    'from dca.config import Settings; Settings()'
  alembic_head "$release" >/dev/null
  chown -R root:"$SERVICE_USER" "$release"
  chmod -R a-w "$release"
  echo "$release"
}

backup_database() {
  local release=$1 label=$2 temporary final
  final="$BACKUP_DIR/$(date -u +%Y%m%dT%H%M%SZ)-${label}.dump"
  temporary="${final}.tmp.$$"
  install -m 0600 /dev/null "$temporary"
  if ! run_with_env "$release" "$release/.venv/bin/python" -c '
import os
import sys
from urllib.parse import parse_qs, unquote, urlsplit
from dca.config import Settings

url = urlsplit(Settings().database_url.replace("postgresql+psycopg://", "postgresql://", 1))
if not url.hostname or not url.path.lstrip("/"):
    raise SystemExit("DCA_DATABASE_URL must contain host and database")
environment = os.environ.copy()
environment.update(
    PGHOST=url.hostname,
    PGPORT=str(url.port or 5432),
    PGDATABASE=unquote(url.path.lstrip("/")),
)
if url.username is not None:
    environment["PGUSER"] = unquote(url.username)
if url.password is not None:
    environment["PGPASSWORD"] = unquote(url.password)
query = parse_qs(url.query)
if query.get("sslmode"):
    environment["PGSSLMODE"] = query["sslmode"][-1]
os.execvpe(
    "pg_dump",
    ["pg_dump", "--format=custom", "--no-owner", "--no-acl", "--file", sys.argv[1]],
    environment,
)
' "$temporary"; then
    rm -f "$temporary"
    return 1
  fi
  mv "$temporary" "$final"
  LAST_BACKUP=$final
}

start_release() {
  local release=$1
  systemctl start dca-api.service
  smoke "$release"
  systemctl start dca-worker.service
  systemctl is-active --quiet "${SERVICES[@]}"
}

stop_services() {
  systemctl stop "${SERVICES[@]}"
}

smoke() {
  local release=$1
  "$release/.venv/bin/python" - "$SMOKE_URL" <<'PY'
import json
import sys
import time
import urllib.error
import urllib.request

url = sys.argv[1]
last_error = "no response"
for _ in range(30):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=5) as response:
            payload = json.load(response)
        checks = payload.get("checks", {})
        if payload.get("status") != "ok":
            raise ValueError("status is not ok")
        if checks.get("database") is not True:
            raise ValueError("database check failed")
        if checks.get("redis") is not True:
            raise ValueError("redis check failed")
        if "telegram" in checks and checks["telegram"].get("ok") is not True:
            raise ValueError("telegram check failed")
        raise SystemExit(0)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        last_error = str(error)
        time.sleep(2)
raise SystemExit(f"smoke failed: {last_error}")
PY
}

recover() {
  local exit_code=$?
  trap - ERR
  set +e
  if [[ $SERVICES_STOPPED -eq 1 ]]; then
    stop_services
    if [[ $MIGRATION_ATTEMPTED -eq 1 ]]; then
      if ! run_with_env "$RECOVERY_RELEASE" \
        "$RECOVERY_RELEASE/.venv/bin/alembic" "$RECOVERY_COMMAND" "$RECOVERY_REVISION"; then
        echo "dca-deploy: database recovery failed; services remain stopped" >&2
        [[ -n $LAST_BACKUP ]] && echo "dca-deploy: backup: $LAST_BACKUP" >&2
        exit "$exit_code"
      fi
    fi
    if [[ -n $RECOVERY_CURRENT ]]; then
      atomic_link "$CURRENT_LINK" "$RECOVERY_CURRENT"
      start_release "$RECOVERY_CURRENT" || true
    else
      rm -f "$CURRENT_LINK"
    fi
  fi
  [[ -n $LAST_BACKUP ]] && echo "dca-deploy: rolled back; backup: $LAST_BACKUP" >&2
  exit "$exit_code"
}

deploy() {
  local release old_release old_revision
  release=$(prepare_release)
  old_release=$(current_target "$CURRENT_LINK")
  old_revision=$(alembic_current "$release")
  old_revision=${old_revision:-base}

  RECOVERY_RELEASE=$release
  RECOVERY_REVISION=$old_revision
  RECOVERY_COMMAND=downgrade
  RECOVERY_CURRENT=$old_release
  trap recover ERR

  stop_services
  SERVICES_STOPPED=1
  backup_database "$release" "before-${release##*/}"
  MIGRATION_ATTEMPTED=1
  run_with_env "$release" "$release/.venv/bin/alembic" upgrade head

  [[ -z $old_release ]] || atomic_link "$PREVIOUS_LINK" "$old_release"
  atomic_link "$CURRENT_LINK" "$release"
  start_release "$release"

  SERVICES_STOPPED=0
  MIGRATION_ATTEMPTED=0
  trap - ERR
  echo "deployed ${release##*/}"
  echo "backup $LAST_BACKUP"
}

rollback() {
  local active target active_revision target_revision
  active=$(current_target "$CURRENT_LINK")
  target=$(current_target "$PREVIOUS_LINK")
  [[ -n $active ]] || die "no active release"
  [[ -n $target ]] || die "no previous release"
  [[ $active != "$target" ]] || die "current and previous releases are identical"
  active_revision=$(alembic_current "$active")
  active_revision=${active_revision:-base}
  target_revision=$(alembic_head "$target")

  RECOVERY_RELEASE=$active
  RECOVERY_REVISION=$active_revision
  RECOVERY_COMMAND=upgrade
  RECOVERY_CURRENT=$active
  trap recover ERR

  stop_services
  SERVICES_STOPPED=1
  backup_database "$active" "before-rollback-${active##*/}"
  MIGRATION_ATTEMPTED=1
  run_with_env "$active" "$active/.venv/bin/alembic" downgrade "$target_revision"

  atomic_link "$CURRENT_LINK" "$target"
  atomic_link "$PREVIOUS_LINK" "$active"
  start_release "$target"

  SERVICES_STOPPED=0
  MIGRATION_ATTEMPTED=0
  trap - ERR
  echo "rolled back to ${target##*/}"
  echo "backup $LAST_BACKUP"
}

status() {
  echo "current:  $(current_target "$CURRENT_LINK")"
  echo "previous: $(current_target "$PREVIOUS_LINK")"
  systemctl --no-pager --full status "${SERVICES[@]}" || true
}

main() {
  local command=${1:-deploy}
  case "$command" in
    deploy | rollback)
      require_root
      require_command flock
      require_command git
      require_command tar
      require_command runuser
      require_command uv
      require_command corepack
      require_command pg_dump
      require_command systemctl
      install -d -m 0750 -o root -g "$SERVICE_USER" "$RELEASES_DIR" "$BACKUP_DIR"
      acquire_lock
      "$command"
      ;;
    smoke)
      smoke "$(current_target "$CURRENT_LINK")"
      ;;
    status)
      status
      ;;
    *)
      die "usage: $0 [deploy|rollback|smoke|status]"
      ;;
  esac
}

main "$@"
