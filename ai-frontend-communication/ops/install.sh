#!/usr/bin/env bash
set -Eeuo pipefail

[[ ${EUID} -eq 0 ]] || { echo "run as root" >&2; exit 1; }

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SOURCE_DIR=${DCA_SOURCE_DIR:-$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)}
APP_DIR="$SOURCE_DIR/ai-frontend-communication"

[[ -x $APP_DIR/ops/deploy.sh ]] || { echo "invalid source checkout: $SOURCE_DIR" >&2; exit 1; }
id dca >/dev/null 2>&1 || useradd --system --home-dir /var/lib/dca --create-home --shell /usr/sbin/nologin dca
install -d -m 0750 -o dca -g dca \
  /var/lib/dca /var/lib/dca/repositories /var/lib/dca/snapshots /var/lib/dca/.cache
install -d -m 0750 -o root -g dca /opt/dca/releases /var/backups/dca
install -d -m 0750 -o root -g dca /etc/dca /etc/dca/keys
install -m 0644 "$APP_DIR/ops/systemd/dca-api.service" /etc/systemd/system/dca-api.service
install -m 0644 "$APP_DIR/ops/systemd/dca-worker.service" /etc/systemd/system/dca-worker.service
ln -sfn "$APP_DIR/ops/deploy.sh" /usr/local/sbin/dca-deploy
systemctl daemon-reload
systemctl enable dca-api.service dca-worker.service

echo "installed; create /etc/dca/dca.env (root:root 0600), then run: dca-deploy deploy"
