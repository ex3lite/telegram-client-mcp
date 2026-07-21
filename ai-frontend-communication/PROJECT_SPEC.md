# Developer Communication Agent: executable MVP contract

## Product boundary

This repository contains a standalone, single-organization service for multiple software
projects. It connects four trust domains: Telegram users, read-only Git snapshots, Claude Code,
and external MCP clients. PostgreSQL is the source of truth. Redis is only a wake-up and rate-limit
helper; deleting Redis must not delete product state.

The MVP is complete only when these four paths work end to end:

1. `/ask` in Telegram produces an answer tied to one repository commit and only cites verified
   file/line ranges from that immutable snapshot.
2. `/request` creates an internal change request visible to an authenticated administrator.
3. An MCP client asks one reachable project member a question, survives process restarts, and polls
   the durable answer later.
4. Every external action and state transition is reconstructable by `correlation_id` in the audit
   view.

## Deliberate MVP decisions

- One Python modular monolith, with API and worker processes built from the same image.
- A PostgreSQL `jobs` table is both durable queue and transactional outbox. Workers claim work with
  `FOR UPDATE SKIP LOCKED`. Redis never owns a job.
- No vector database or persistent knowledge index. `/ask` searches an exact Git commit on demand.
- No Celery, Kafka, WebSocket, S3, provider framework, prompt editor, or generic event bus.
- Telegram is command/reply driven. The bot does not classify every group message with an LLM.
- A timed-out Telegram send becomes `delivery_uncertain`; it is not blindly retried because the
  remote side may already have accepted it.
- Human answers, repository files, and repository-local agent instructions are untrusted data.
- Claude receives an extracted commit snapshot and only the Read, Glob, and Grep tools. Project
  hooks, project MCP servers, writes, sessions, and shell execution are disabled.
- The initial admin surface is an operational queue: overview, clarifications, requests,
  repositories, audit, and settings. Charts and decorative dashboards are excluded.

## State machines

Clarification state is independent from delivery state:

```text
pending -> answered
pending -> expired
pending -> cancelled
```

The first valid human reply wins through a conditional database update. A reply is invalid when
the request is expired, cancelled, already answered, sent by another Telegram identity, or does not
reference the bound Telegram message/request.

Change request state:

```text
open -> in_progress -> done
open -> rejected
in_progress -> rejected
```

Every mutation uses an expected version to prevent silent concurrent overwrites.

Job state:

```text
queued -> running -> succeeded
queued -> running -> retry -> running
queued -> running -> failed
queued -> running -> delivery_uncertain
```

## Security and provenance invariants

- Telegram webhooks require `X-Telegram-Bot-Api-Secret-Token`, enforce a body limit, and deduplicate
  `update_id` before work is queued.
- MCP service accounts are scoped by tool and project. Secrets are stored as Argon2id hashes and
  are never returned after creation.
- Telegram identities are internal bindings, not primary user IDs.
- Deploy keys are per repository, read-only, and used only by the fetch process with pinned host
  keys. Claude never receives an SSH key.
- A citation is publishable only if its relative path stays inside the snapshot, the file exists,
  and `1 <= start_line <= end_line <= file_line_count`.
- The response records repository, commit, Claude CLI version, structured output, accepted
  citations, rejected citations, and uncertainty.
- Secrets and raw credentials are never written to audit payloads or application logs.

## Release gates

- Unit tests cover state transitions, citation validation, redaction, and idempotency decisions.
- PostgreSQL integration tests cover concurrent replies, webhook deduplication, job restart, and
  cross-project service-account denial.
- Contract tests cover Telegram Bot API payloads and MCP schemas.
- `ruff`, strict `mypy`, `pytest`, frontend typecheck, frontend build, and clean-database migration
  all pass.
- A manual smoke run demonstrates the four paths above against a test Telegram bot and a read-only
  test repository.
- The deployment satisfies the production checklist in [docs/OPERATIONS.md](docs/OPERATIONS.md),
  including HTTPS, secret rotation, backup/restore, pinned Git host keys, and live Telegram checks.
- Telegram behavior matches the implemented/deferred matrix in
  [docs/TELEGRAM_10_2.md](docs/TELEGRAM_10_2.md).
