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

## Operator control plane

The panel at `/` is the working control plane, not only a health dashboard. Per project it controls
the Claude model, effort, timeout, budget, base prompt, answer style, bounded conversation memory,
Telegram response modes, native AI drafts and Markdown attachments. Claude OAuth can be completed
from a guided panel flow; the resulting credential is encrypted at rest and is never returned to
the browser. An environment credential remains a supported fallback.

Privacy is enforced after model output and before every MCP-originated Telegram delivery. `strict`
blocks suspected credentials before persistence or delivery, while `balanced` persists and sends
only redacted content. Repository snapshots always exclude built-in secret globs, with additional
project deny globs configured in the panel.

MCP service accounts are managed from the same panel with project and tool scopes, expiry,
deactivation and one-time token rotation. Besides durable clarifications they can send idempotent
informational messages and Markdown documents without receiving the Telegram bot token.
The `memory_get_context` tool exposes only bounded, privacy-sanitized history for an explicitly
scoped project member or chat.

## Durable agent memory

PostgreSQL stores isolated conversation threads, redacted messages, rolling summaries and durable
facts. Private conversations are scoped by project and employee; group conversations additionally
include the configured internal chat. Before each Claude run the worker injects a bounded summary
and recent history as untrusted data, then persists the privacy-checked answer and updated summary.
The panel's **Память** screen lets administrators inspect or delete a complete thread.

Private Telegram chats use Bot API Rich Message drafts with a stable non-zero draft ID and a
server-controlled Thinking block. Raw model deltas are never published before the privacy filter;
the permanent Rich Message is sent only after the final answer has passed policy checks.

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
