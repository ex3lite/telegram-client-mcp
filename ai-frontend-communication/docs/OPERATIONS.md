# Production operations

Production runs without Docker: PostgreSQL, Redis, API and worker are native systemd services.
The dedicated agency vhost on the existing edge nginx proxies the frontend and DCA routes to
`172.18.0.1:8000`.
Docker Compose remains a local-development option and is not part of the release path.

## Host prerequisites

- Ubuntu 24.04, PostgreSQL 16, Redis 7, Git, `pg_dump`, Node 22/Corepack and `uv`.
- Claude Code installed from Anthropic's signed stable APT repository.
- The dedicated public URL `https://agency.kakaduai.com`, Telegram bot token, Claude subscription
  token and an HTTP(S) CONNECT proxy.
- A read-only Git deploy key for every repository exposed to the agent.

On `test_ai`, native PostgreSQL and Redis use `127.0.0.1:5433` and `127.0.0.1:6380` so the
existing container services on 5432/6379 remain untouched. The edge is the
`kakadudocs-nginx-1` container. It has one relevant bind mount:

```text
/srv/kakadudocs/deploy/nginx/kakaduai.com.conf
  -> /etc/nginx/conf.d/default.conf
```

Therefore `ops/nginx/agency.kakaduai.com.conf` is an appendable server-block artifact, not a file
that nginx discovers automatically. `kakaduai.com` remains the apex product site and must not
serve DCA. The agency TLS block uses the existing `timed_json` log format, permits only TLS 1.2/1.3
and denies framing through both `X-Frame-Options` and CSP. Its `/` fallback proxies the panel at
the domain root; `/admin` is not the production panel URL.
Allow only the nginx bridge to reach the native API:

```bash
ufw allow from 172.18.0.0/16 to 172.18.0.1 port 8000 proto tcp
```

Issue the certificate through the webroot already shared with edge nginx:

```bash
certbot certonly --webroot --webroot-path /var/www/certbot \
  --domain agency.kakaduai.com
```

Run that command in the existing Certbot execution context so `/var/www/certbot` and
`/etc/letsencrypt` are the same volumes seen by nginx. Do not append the TLS server block until the
certificate files exist.

Safely merge the two server blocks from `ops/nginx/agency.kakaduai.com.conf` into the mounted host
file. Keep the existing inode: edit or append in place; do not replace the bind-mounted file. Take
a backup first and refuse a duplicate agency block:

```bash
cd /srv/kakadudocs
config=deploy/nginx/kakaduai.com.conf
artifact=/opt/dca/source/ai-frontend-communication/ops/nginx/agency.kakaduai.com.conf
backup_dir=/var/backups/kakadudocs/nginx
install -d -m 0700 "$backup_dir"
backup="$backup_dir/kakaduai.com.conf.$(date -u +%Y%m%d_%H%M%S).before-agency"

if grep -q 'server_name agency\.kakaduai\.com;' "$config"; then
  echo 'agency server block already exists; merge manually' >&2
  exit 1
fi
cp -a "$config" "$backup"
printf '\n' | tee -a "$config" >/dev/null
tee -a "$config" < "$artifact" >/dev/null

if ! docker compose exec -T nginx nginx -t; then
  cp "$backup" "$config"
  docker compose exec -T nginx nginx -t
  exit 1
fi
docker compose exec -T nginx nginx -s reload
```

No container recreation is needed for an in-place config edit. Keep all old apex DCA routes during
this transitional step. Remove them only after completing the agency cutover below.

Install the versioned deploy hook on the Docker host. Certbot's renewal timer runs executable
files in this directory after a certificate is actually renewed; the hook validates nginx before
reloading the exact edge container:

```bash
install -m 0755 \
  /opt/dca/source/ai-frontend-communication/ops/certbot/agency-nginx-reload.sh \
  /etc/letsencrypt/renewal-hooks/deploy/agency-nginx-reload

# Validate the installed hook once; a failed nginx -t prevents reload.
/etc/letsencrypt/renewal-hooks/deploy/agency-nginx-reload
```

The production endpoints are:

```text
Frontend: https://agency.kakaduai.com/
Webhook compatibility: https://agency.kakaduai.com/webhooks/telegram
MCP:      https://agency.kakaduai.com/mcp
Health:   https://agency.kakaduai.com/health/ready?deep=true
```

Claude Code supports HTTP/HTTPS proxy environment variables, not SOCKS. Set one secret
`DCA_OUTBOUND_PROXY_URL`; the application passes only that URL to both Claude and aiogram. It does
not copy the full service environment into the Claude subprocess. Percent-encode proxy credentials
and never commit them.

## Initial installation

Clone the repository into the fixed deployment checkout and install the units:

```bash
git clone git@github.com:ex3lite/telegram-client-mcp.git /opt/dca/source
/opt/dca/source/ai-frontend-communication/ops/install.sh
install -m 0600 \
  /opt/dca/source/ai-frontend-communication/ops/dca.env.example \
  /etc/dca/dca.env
```

Fill `/etc/dca/dca.env`. Required secrets are the database password, Telegram token, a random
session/HMAC secret of at least 32 characters, Claude OAuth token and outbound proxy URL. Production
uses `DCA_TELEGRAM_MODE=polling`; the webhook secret is required only for `webhook` mode. Keep the
file `root:root 0600`.

Create the `dca` database and least-privilege login, then run the first release:

```bash
# One-time transition when dca-deploy may still point at the pre-root-layout script.
sudo git -C /opt/dca/source pull --ff-only origin main
sudo /opt/dca/source/ai-frontend-communication/ops/install.sh

sudo dca-deploy deploy
```

The preliminary pull/install is required only once for an existing installation: the old running
wrapper cannot acquire behavior from code it has not pulled yet. The dedicated source checkout
must be clean before this command. Routine deploys pull internally.

Create each panel principal after the schema is deployed. The command prints a generated UUID key
only on creation; only its HMAC-SHA-256 fingerprint is stored:

```bash
sudo dca-deploy bootstrap admin-key --name "Owner"

# Revoke every key and server-side session for a principal, or one exact key:
sudo dca-deploy bootstrap admin-key-revoke --name "Owner"
sudo dca-deploy bootstrap admin-key-revoke --key-id <internal-key-id>
```

Revocation deactivates the selected key(s), revokes their server-side admin sessions and writes an
audit event. Logout revokes the current server-side session as well.

The deploy refuses a dirty source checkout, uses only a fast-forward pull, archives the exact Git
commit into an immutable release directory, installs from both lock files, backs up PostgreSQL,
applies migrations, atomically switches the current symlink, checks API/database/Redis/Telegram,
then starts the worker.

## Routine release and rollback

These are the complete one-command entry points suitable for an operator or deployment agent:

```bash
sudo dca-deploy deploy
sudo dca-deploy rollback
sudo dca-deploy bootstrap <dca-bootstrap command> [arguments]
sudo dca-deploy status
```

`flock` prevents concurrent releases. A failed migration leaves the prior release active. A failed
smoke check runs the matching down migration before switching back. If database recovery itself
fails, both application services stay stopped and the script prints the fresh dump path; it never
performs an automatic destructive restore. If the restored release cannot start or pass smoke,
the command reports `OUTAGE`, prints both systemd service states and exits nonzero. During the root
cutover, frontend smoke accepts `/` first and legacy `/admin/` only as a temporary fallback.

`dca-deploy bootstrap` takes the same lock as deploy/rollback. Root parses the `root:root 0600`
environment file without shell evaluation, then runs only the fixed `dca-bootstrap` entrypoint as
`dca` through `runuser --preserve-environment` after sanitizing the parent environment. Do not call
the venv command directly with `sudo -u dca`: that user cannot and should not read
`/etc/dca/dca.env`.

## Agency cutover

First add and reload the agency server blocks as described above while the old apex DCA routes are
still present. Then, from a root shell (`sudo -i`), atomically change the application origin:

```bash
set -Eeuo pipefail
env_file=/etc/dca/dca.env
env_tmp=$(mktemp "${env_file}.XXXXXX")
trap 'rm -f "$env_tmp"' EXIT
sed 's#^DCA_PUBLIC_URL=.*#DCA_PUBLIC_URL=https://agency.kakaduai.com#' \
  "$env_file" > "$env_tmp"
test "$(grep -c '^DCA_PUBLIC_URL=' "$env_tmp")" -eq 1
if grep -q '^DCA_TELEGRAM_MODE=' "$env_tmp"; then
  sed -i 's/^DCA_TELEGRAM_MODE=.*/DCA_TELEGRAM_MODE=polling/' "$env_tmp"
else
  printf '\nDCA_TELEGRAM_MODE=polling\n' >> "$env_tmp"
fi
chown root:root "$env_tmp"
chmod 0600 "$env_tmp"
mv -f "$env_tmp" "$env_file"
trap - EXIT

dca-deploy deploy
telegram_setup=$(dca-deploy bootstrap telegram-setup)
printf '%s\n' "$telegram_setup"
grep -Fxq 'telegram_mode=polling' <<<"$telegram_setup"
grep -Fxq 'webhook=deleted' <<<"$telegram_setup"
```

`telegram-setup` performs `getMe` and `deleteWebhook(drop_pending_updates=false)` through
`DCA_OUTBOUND_PROXY_URL`; the worker then long-polls through the same proxy. The deep health check
below repeats the proxy-aware Telegram check. Verify the panel root, worker service health,
authenticated MCP origin and browser same-origin boundary before touching apex. Set the MCP token
printed by `seed` and an admin UUID printed by `admin-key` in the current shell only:

```bash
: "${DCA_MCP_TOKEN:?set the MCP service-account token}"
: "${DCA_ADMIN_ACCESS_KEY:?set an admin access UUID}"

curl --fail --silent --show-error https://agency.kakaduai.com/ \
  | grep -q '<div id="app"></div>'
curl --fail --silent --show-error https://agency.kakaduai.com/health/live
curl --fail --silent --show-error \
  'https://agency.kakaduai.com/health/ready?deep=true'

curl --fail --silent --show-error \
  --header "Authorization: Bearer $DCA_MCP_TOKEN" \
  --header 'Origin: https://agency.kakaduai.com' \
  --header 'Content-Type: application/json' \
  --header 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"cutover-check","version":"1"}}}' \
  https://agency.kakaduai.com/mcp >/dev/null

cookie_jar=$(mktemp)
trap 'rm -f "$cookie_jar"' EXIT
curl --fail --silent --show-error --cookie-jar "$cookie_jar" \
  --header 'Content-Type: application/json' \
  --data "{\"access_key\":\"$DCA_ADMIN_ACCESS_KEY\"}" \
  https://agency.kakaduai.com/api/v1/auth/login >/dev/null
test "$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --cookie "$cookie_jar" --header 'Origin: https://kakaduai.com' \
  --request POST https://agency.kakaduai.com/api/v1/auth/logout)" = 403
curl --fail --silent --show-error --cookie "$cookie_jar" \
  --header 'Origin: https://agency.kakaduai.com' --request POST \
  https://agency.kakaduai.com/api/v1/auth/logout >/dev/null
rm -f "$cookie_jar"
trap - EXIT
```

Only now remove the old exact DCA locations and their old named proxy location from the apex
server. Do not remove the apex server itself:

```bash
cd /srv/kakadudocs
config=deploy/nginx/kakaduai.com.conf
backup_dir=/var/backups/kakadudocs/nginx
install -d -m 0700 "$backup_dir"
cp -a "$config" \
  "$backup_dir/kakaduai.com.conf.$(date -u +%Y%m%d_%H%M%S).before-apex-removal"
# Edit $config in place; remove only the old DCA locations from the apex server.
docker compose exec -T nginx nginx -t
docker compose exec -T nginx nginx -s reload
curl --fail --silent --show-error https://agency.kakaduai.com/ >/dev/null
curl --fail --silent --show-error https://kakaduai.com/ >/dev/null
```

## SQL migration contract

Alembic owns revision ordering and `alembic_version`. Every revision has both files:

```text
migrations/sql/<revision>.up.sql
migrations/sql/<revision>.down.sql
```

The legacy revisions `744685d9ddd2` and `91d7cfe41a2b` retain their existing names. Every new
revision uses the UTC creation time plus a lowercase slug, for example:

```text
migrations/sql/20260721_213045_add_member_status.up.sql
migrations/sql/20260721_213045_add_member_status.down.sql
```

The SQL files contain no `BEGIN`, `COMMIT`, `ROLLBACK`, psql commands, `VACUUM`, `CREATE DATABASE`
or concurrent index creation. Alembic runs the whole upgrade/downgrade under PostgreSQL
transactional DDL and an advisory transaction lock. CI validates that every Python revision has
one complete SQL pair. The down file must restore exactly the schema expected by `down_revision`.

## Bootstrap Telegram and the team

Create the project and MCP service account, then link verified users and the real Telegram group:

```bash
sudo dca-deploy bootstrap seed \
  --project-slug backend --project-name Backend

sudo dca-deploy bootstrap link-user \
  --project-slug backend --name "Developer" --telegram-user-id 123456789 \
  --role android --department Mobile --stack "Android / Kotlin" --verify

sudo dca-deploy bootstrap link-chat \
  --project-slug backend --telegram-chat-id -1001234567890

sudo dca-deploy bootstrap telegram-setup
```

`department` and `stack` are project-scoped server metadata exposed to MCP and supplied to Claude
for Telegram questions. A linked Telegram member is not an admin-panel account: panel access is
controlled only by separate `admin_principals` and revocable `admin_access_keys` records.

The service-account token is printed only when first created; store it immediately. Every person
must open the bot and run `/start` before private delivery is possible. Validate the group ID with
Bot API `getChat` after adding the bot: supergroup IDs normally have the `-100...` form, and an ID
that returns `chat not found` must not be seeded.

## Release gates

- `ruff`, strict `mypy`, all unit/integration tests, frontend typecheck/build and frozen lock sync.
- Empty-database upgrade and full downgrade through the raw SQL pair.
- `ops/test-deploy.sh`, native service status, and public `/health/live` and
  `/health/ready?deep=true` on `agency.kakaduai.com`.
- Telegram `getMe`, deleted webhook, active polling worker and a real group/private round trip
  through the configured proxy.
- Claude answer from an immutable snapshot with a server-verified file/line citation.
- MCP bearer authentication and audit reconstruction by `correlation_id`.
- Fresh PostgreSQL dump and a rehearsed manual restore procedure.

Never commit `/etc/dca/dca.env`, deploy keys, OAuth tokens, bot tokens, database dumps or generated
service-account tokens.
