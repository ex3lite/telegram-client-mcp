#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=deploy.sh
source "$SCRIPT_DIR/deploy.sh"
bash -n "$SCRIPT_DIR/certbot/agency-nginx-reload.sh"
grep -Fxq 'KillSignal=SIGINT' "$SCRIPT_DIR/systemd/dca-worker.service"
grep -Fxq 'InaccessiblePaths=/etc/dca' "$SCRIPT_DIR/systemd/dca-worker.service"

sandbox=$(mktemp -d)
trap 'rm -rf -- "$sandbox"' EXIT

valid="$sandbox/valid/migrations/sql"
mkdir -p "$valid"
touch \
  "$valid/744685d9ddd2.up.sql" "$valid/744685d9ddd2.down.sql" \
  "$valid/91d7cfe41a2b.up.sql" "$valid/91d7cfe41a2b.down.sql" \
  "$valid/20260721_213045_add_profile.up.sql" \
  "$valid/20260721_213045_add_profile.down.sql"
validate_sql_pairs "$sandbox/valid"

expect_invalid() {
  local name=$1
  local directory="$sandbox/$name/migrations/sql"
  mkdir -p "$directory"
  shift
  touch "$directory/$1.up.sql" "$directory/$1.down.sql"
  if (validate_sql_pairs "$sandbox/$name") >/dev/null 2>&1; then
    echo "expected invalid migration: $1" >&2
    exit 1
  fi
}

expect_invalid legacy_hash abcdef123456
expect_invalid invalid_time 20260721_246000_bad_time

missing="$sandbox/missing/migrations/sql"
mkdir -p "$missing"
touch "$missing/20260721_213046_missing_down.up.sql"
if (validate_sql_pairs "$sandbox/missing") >/dev/null 2>&1; then
  echo "expected missing down migration to fail" >&2
  exit 1
fi

env_release="$sandbox/env-release"
mkdir -p "$env_release/.venv/bin"
ln -s "$(command -v python3)" "$env_release/.venv/bin/python"
printf '%s\n' 'DCA_TEST_LITERAL=$argon2id$v=19' > "$sandbox/dca.env"
(
  ENV_FILE="$sandbox/dca.env"
  export DCA_PARENT_ONLY=must_not_leak
  run_with_env "$env_release" python3 -c \
    'import os; assert os.environ["DCA_TEST_LITERAL"] == "$argon2id$v=19"; assert "DCA_PARENT_ONLY" not in os.environ'
)

(
  SMOKE_FRONTEND_URL=http://root/
  SMOKE_FRONTEND_LEGACY_URL=http://legacy/admin/
  curl() {
    local argument last=
    for argument in "$@"; do last=$argument; done
    if [[ $last == "$SMOKE_FRONTEND_LEGACY_URL" ]]; then
      printf '<div id="app"></div>'
    else
      printf 'legacy transition'
    fi
  }
  smoke_frontend
  curl() { return 22; }
  if smoke_frontend; then
    echo "frontend smoke accepted two failed routes" >&2
    exit 1
  fi
)

(
  SERVICES_STOPPED=0
  stop_services() {
    [[ $SERVICES_STOPPED -eq 1 ]]
    return 23
  }
  if stop_for_release_change; then
    echo "partial stop test unexpectedly succeeded" >&2
    exit 1
  fi
  [[ $SERVICES_STOPPED -eq 1 ]]
)

(
  start_release() { return 19; }
  systemctl() {
    echo 'service-state-visible' >&2
    return 3
  }
  if recovery_output=$(restart_recovered_release /release 2>&1); then
    echo "failed recovery restart was swallowed" >&2
    exit 1
  fi
  grep -q 'OUTAGE' <<<"$recovery_output"
  grep -q 'service-state-visible' <<<"$recovery_output"
)

(
  current_target() { printf '/opt/dca/releases/current'; }
  runuser() { :; }
  captured=()
  run_with_env() { captured=("$@"); }
  bootstrap admin-key --name 'Backend Owner'
  joined=$(printf '<%s>' "${captured[@]}")
  [[ ${captured[0]} == /opt/dca/releases/current ]]
  [[ $joined == *'<--preserve-environment>'* ]]
  [[ $joined == *'</opt/dca/releases/current/.venv/bin/dca-bootstrap>'* ]]
  [[ $joined == *'<admin-key><--name><Backend Owner>'* ]]
)

(
  lock_acquired=0
  bootstrap_called=0
  require_root() { :; }
  require_command() { :; }
  acquire_lock() { lock_acquired=1; }
  bootstrap() {
    [[ $lock_acquired -eq 1 ]]
    [[ $1 == telegram-setup ]]
    bootstrap_called=1
  }
  main bootstrap telegram-setup
  [[ $bootstrap_called -eq 1 ]]
)

echo "deploy operational contracts: ok"
