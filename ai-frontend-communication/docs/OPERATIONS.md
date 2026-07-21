# Production operations

Production runs without Docker: PostgreSQL, Redis, API and worker are native systemd services.
The existing edge nginx terminates TLS and proxies only the DCA routes to `172.18.0.1:8000`.
Docker Compose remains a local-development option and is not part of the release path.

## Host prerequisites

- Ubuntu 24.04, PostgreSQL 16, Redis 7, Git, `pg_dump`, Node 22/Corepack and `uv`.
- Claude Code installed from Anthropic's signed stable APT repository.
- A public HTTPS URL, Telegram bot token, Claude subscription token and an HTTP(S) CONNECT proxy.
- A read-only Git deploy key for every repository exposed to the agent.

On `test_ai`, native PostgreSQL and Redis use `127.0.0.1:5433` and `127.0.0.1:6380` so the
existing container services on 5432/6379 remain untouched. The nginx locations from
`ops/nginx/dca-apex.locations.conf` belong inside the HTTPS `kakaduai.com` server block.

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

Fill `/etc/dca/dca.env`. Required secrets are the database password, Telegram token and webhook
secret, Argon2id admin password hash, independent session secret, Claude OAuth token and outbound
proxy URL. Generate the admin hash with `dca-bootstrap hash-password` in a built release. Keep the
file `root:root 0600`.

Create the `dca` database and least-privilege login, then run the first release:

```bash
dca-deploy deploy
```

The deploy refuses a dirty source checkout, uses only a fast-forward pull, archives the exact Git
commit into an immutable release directory, installs from both lock files, backs up PostgreSQL,
applies migrations, atomically switches the current symlink, checks API/database/Redis/Telegram,
then starts the worker.

## Routine release and rollback

These are the complete one-command entry points suitable for an operator or deployment agent:

```bash
sudo dca-deploy deploy
sudo dca-deploy rollback
sudo dca-deploy status
```

`flock` prevents concurrent releases. A failed migration leaves the prior release active. A failed
smoke check runs the matching down migration before switching back. If database recovery itself
fails, both application services stay stopped and the script prints the fresh dump path; it never
performs an automatic destructive restore.

## SQL migration contract

Alembic owns revision ordering and `alembic_version`. Every revision has both files:

```text
migrations/sql/<revision>.up.sql
migrations/sql/<revision>.down.sql
```

The SQL files contain no `BEGIN`, `COMMIT`, `ROLLBACK`, psql commands, `VACUUM`, `CREATE DATABASE`
or concurrent index creation. Alembic runs the whole upgrade/downgrade under PostgreSQL
transactional DDL and an advisory transaction lock. CI validates that every Python revision has
one complete SQL pair. The down file must restore exactly the schema expected by `down_revision`.

## Bootstrap Telegram and the team

Create the project and MCP service account, then link verified users and the real Telegram group:

```bash
sudo -u dca /opt/dca/current/.venv/bin/dca-bootstrap seed \
  --project-slug backend --project-name Backend

sudo -u dca /opt/dca/current/.venv/bin/dca-bootstrap link-user \
  --project-slug backend --name "Developer" --telegram-user-id 123456789 \
  --role backend --verify

sudo -u dca /opt/dca/current/.venv/bin/dca-bootstrap link-chat \
  --project-slug backend --telegram-chat-id -1001234567890

sudo -u dca /opt/dca/current/.venv/bin/dca-bootstrap telegram-setup
```

The service-account token is printed only when first created; store it immediately. Every person
must open the bot and run `/start` before private delivery is possible. Validate the group ID with
Bot API `getChat` after adding the bot: supergroup IDs normally have the `-100...` form, and an ID
that returns `chat not found` must not be seeded.

## Release gates

- `ruff`, strict `mypy`, all unit/integration tests, frontend typecheck/build and frozen lock sync.
- Empty-database upgrade and full downgrade through the raw SQL pair.
- Native service status plus public `/health/live` and `/health/ready?deep=true`.
- Telegram `getMe`, webhook state and a real group/private round trip through the configured proxy.
- Claude answer from an immutable snapshot with a server-verified file/line citation.
- MCP bearer authentication and audit reconstruction by `correlation_id`.
- Fresh PostgreSQL dump and a rehearsed manual restore procedure.

Never commit `/etc/dca/dca.env`, deploy keys, OAuth tokens, bot tokens, database dumps or generated
service-account tokens.
