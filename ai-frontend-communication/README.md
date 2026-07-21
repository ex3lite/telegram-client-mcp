# Developer Communication Agent

A standalone Telegram and MCP bridge for software teams. It turns code questions, change requests,
and human clarifications into durable, auditable workflows.

The current implementation targets a single organization with multiple projects. The detailed MVP
contract and trust boundaries live in [PROJECT_SPEC.md](PROJECT_SPEC.md).

## Stack

- Python 3.13+, FastAPI, aiogram 3.30, SQLAlchemy 2, PostgreSQL, Redis
- MCP Streamable HTTP
- Claude Code CLI from Anthropic's signed stable APT repository, against immutable Git snapshots
- Vue 3, TypeScript, Vite, TanStack Query
- Native systemd production services with transactional SQL up/down migrations
- Docker Compose only for local development

## Local quick start

```bash
cp .env.example .env
uv sync
```

Generate `DCA_SESSION_SECRET` with `openssl rand -hex 32`, fill the remaining secrets, then start
the stack, create the first UUID admin key and seed the first project:

```bash
docker compose config --quiet
docker compose up -d --build
docker compose run --rm --no-deps api uv run --no-sync dca-bootstrap admin-key \
  --name "Owner"
docker compose run --rm --no-deps api uv run --no-sync dca-bootstrap seed \
  --project-slug backend \
  --project-name "Backend"
```

The `seed` command prints the first MCP service-account token once; store it in a secret manager.
Admin cookies contain only a server-side session ID. Revoke all keys and sessions for a principal,
or one internal key, with:

```bash
docker compose run --rm --no-deps api uv run --no-sync dca-bootstrap \
  admin-key-revoke --name "Owner"
docker compose run --rm --no-deps api uv run --no-sync dca-bootstrap \
  admin-key-revoke --key-id <admin-access-key-id>
```

Register the Telegram command menu and configure the selected update transport:

```bash
docker compose run --rm --no-deps api \
  uv run --no-sync dca-bootstrap telegram-setup
```

Production updates and one-step rollback are deliberately short:

```bash
sudo dca-deploy deploy
sudo dca-deploy rollback
```

The admin UI is served at `/`. The API also exposes `/health/live`, `/health/ready`,
`/api/v1`, an optional Telegram webhook at `/webhooks/telegram`, and Streamable HTTP MCP at `/mcp`.
Repository-key provisioning, user linking, deployment lifecycle, and release gates are in
[docs/OPERATIONS.md](docs/OPERATIONS.md). The exact Telegram feature matrix is in
[docs/TELEGRAM_10_2.md](docs/TELEGRAM_10_2.md).

## Checks

```bash
uv run ruff check .
uv run mypy src
uv run pytest
pnpm --dir web install
pnpm --dir web typecheck
pnpm --dir web build
```
