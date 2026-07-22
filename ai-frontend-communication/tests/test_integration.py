from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import orjson
import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient
from itsdangerous import URLSafeTimedSerializer
from pydantic import SecretStr
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url

import dca.worker as worker_module
from dca.app import create_app
from dca.bootstrap import revoke_admin_key, upsert_admin_key
from dca.config import Settings
from dca.db import (
    AdminAccessKey,
    AdminPrincipal,
    AdminSession,
    AuditEvent,
    Clarification,
    Database,
    Job,
    Project,
    ProjectMembership,
    Repository,
    ServiceAccount,
    ServiceAccountProject,
    TelegramIdentity,
    TelegramUpdate,
    User,
    enqueue_repository_sync,
)
from dca.domain import AskUserInput, ClarificationStatus, JobStatus, RepositoryStatus, utcnow
from dca.service import (
    ServiceError,
    admin_key_fingerprint,
    answer_clarification_from_telegram,
    cancel_clarification,
    create_clarification,
    require_service_scope,
)
from dca.telegram import TelegramAdapter, queue_telegram_update, reserve_telegram_update
from dca.worker import Worker

pytestmark = pytest.mark.integration


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    database_url = os.environ.get("DCA_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("DCA_TEST_DATABASE_URL is not set")
    parsed = make_url(database_url)
    if parsed.host not in {"127.0.0.1", "localhost"} or parsed.port != 55432:
        pytest.fail("integration tests only accept the dedicated local PostgreSQL on port 55432")
    database = Database(Settings(database_url=database_url))
    yield database
    async with database.session() as session:
        await session.execute(
            text(
                "TRUNCATE agent_messages, system_secrets, project_agent_settings, admin_sessions, "
                "admin_access_keys, admin_principals, audit_events, interactions, "
                "telegram_identities, telegram_chats, "
                "service_account_projects, repositories, project_memberships, clarifications, "
                "change_requests, telegram_updates, users, service_accounts, projects, jobs "
                "RESTART IDENTITY CASCADE"
            )
        )
    await database.close()


async def seed_scope(database: Database) -> dict[str, Any]:
    async with database.session() as session:
        project = Project(slug=f"project-{uuid4().hex[:8]}", name="Backend")
        other_project = Project(slug=f"other-{uuid4().hex[:8]}", name="Other")
        user = User(display_name="Developer")
        account = ServiceAccount(
            name=f"agent-{uuid4().hex[:8]}",
            token_prefix=uuid4().hex[:8],
            token_hash=PasswordHasher().hash("x" * 40),
            tool_scopes=[
                "telegram.ask_user",
                "telegram.get_clarification",
                "telegram.cancel_clarification",
            ],
        )
        session.add_all([project, other_project, user, account])
        await session.flush()
        session.add_all(
            [
                ProjectMembership(project_id=project.id, user_id=user.id, role="developer"),
                TelegramIdentity(
                    user_id=user.id,
                    telegram_user_id=9001,
                    private_chat_id=9001,
                    verified_at=utcnow(),
                    reachable=True,
                ),
                ServiceAccountProject(
                    service_account_id=account.id,
                    project_id=project.id,
                ),
            ]
        )
    return {
        "project_id": project.id,
        "other_project_id": other_project.id,
        "user_id": user.id,
        "account_id": account.id,
    }


@pytest.mark.asyncio
async def test_admin_key_bootstrap_is_idempotent_and_renames_without_reprinting_secret(
    database: Database,
    capsys: pytest.CaptureFixture[str],
) -> None:
    access_key = uuid4()
    settings = Settings(
        database_url=os.environ["DCA_TEST_DATABASE_URL"],
        session_secret=SecretStr("integration-admin-session-secret"),
    )
    name = f"Owner {uuid4().hex[:8]}"
    args = SimpleNamespace(name=name, access_key=access_key)

    await upsert_admin_key(args, settings)
    created_output = capsys.readouterr().out
    assert str(access_key) in created_output

    await upsert_admin_key(args, settings)
    repeated_output = capsys.readouterr().out
    assert str(access_key) not in repeated_output

    args.name = f"Renamed {uuid4().hex[:8]}"
    await upsert_admin_key(args, settings)
    renamed_output = capsys.readouterr().out
    assert str(access_key) not in renamed_output

    async with database.session() as session:
        key = await session.scalar(
            select(AdminAccessKey).where(
                AdminAccessKey.fingerprint
                == admin_key_fingerprint(access_key, settings.session_secret.get_secret_value())
            )
        )
        assert key is not None
        principal = await session.get(AdminPrincipal, key.principal_id)
        assert principal is not None
        assert principal.name == args.name
        admin_session = AdminSession(
            access_key_id=key.id,
            expires_at=utcnow() + timedelta(days=1),
        )
        session.add(admin_session)
        await session.flush()
        key_id = key.id
        session_id = admin_session.id

    await revoke_admin_key(SimpleNamespace(name=args.name, key_id=None), settings)
    revoked_output = capsys.readouterr().out
    assert f"revoked_admin_access_key_id={key_id}" in revoked_output

    async with database.session() as session:
        revoked_key = await session.get(AdminAccessKey, key_id)
        revoked_session = await session.get(AdminSession, session_id)
        assert revoked_key is not None and revoked_key.active is False
        assert revoked_session is not None and revoked_session.revoked_at is not None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.event_type == "admin.access_key_revoked",
                    AuditEvent.subject_id == str(key_id),
                )
            )
            == 1
        )

    args.access_key = None
    await upsert_admin_key(args, settings)
    rotated_output = capsys.readouterr().out
    assert "Store this UUID now" in rotated_output

    async with database.session() as session:
        keys = list(
            await session.scalars(
                select(AdminAccessKey).where(AdminAccessKey.principal_id == principal.id)
            )
        )
        assert len(keys) == 2
        assert sum(key.active for key in keys) == 1
        assert next(key for key in keys if key.id == key_id).active is False


@pytest.mark.asyncio
async def test_clarification_idempotency_and_first_reply_wins(database: Database) -> None:
    ids = await seed_scope(database)
    request = AskUserInput(
        project_id=ids["project_id"],
        agent_run_id="run-1",
        correlation_id="corr-1",
        idempotency_key="idem-key-123",
        recipient_user_id=ids["user_id"],
        context="Need a default TTL",
        question="Should the default be 24h or 7d?",
        expires_at=utcnow() + timedelta(hours=1),
    )
    async with database.session() as session:
        first, first_created = await create_clarification(
            session, service_account_id=ids["account_id"], request=request
        )
        second, second_created = await create_clarification(
            session, service_account_id=ids["account_id"], request=request
        )
        assert first.id == second.id
        assert first_created is True
        assert second_created is False
        with pytest.raises(ServiceError) as payload_conflict:
            await create_clarification(
                session,
                service_account_id=ids["account_id"],
                request=request.model_copy(update={"question": "A different question"}),
            )
        assert payload_conflict.value.code == "idempotency_conflict"
        session.add_all(
            [
                ProjectMembership(
                    project_id=ids["other_project_id"],
                    user_id=ids["user_id"],
                    role="developer",
                ),
                ServiceAccountProject(
                    service_account_id=ids["account_id"],
                    project_id=ids["other_project_id"],
                ),
            ]
        )
        with pytest.raises(ServiceError) as project_conflict:
            await create_clarification(
                session,
                service_account_id=ids["account_id"],
                request=request.model_copy(update={"project_id": ids["other_project_id"]}),
            )
        assert project_conflict.value.code == "idempotency_conflict"
        first.telegram_chat_id = 9001
        first.telegram_message_id = 44
    async with database.session() as session:
        assert await session.scalar(select(func.count()).select_from(Job)) == 1

    async def reply(value: str) -> str:
        try:
            async with database.session() as session:
                await answer_clarification_from_telegram(
                    session,
                    telegram_user_id=9001,
                    telegram_chat_id=9001,
                    reply_to_message_id=44,
                    answer=value,
                )
            return "won"
        except ServiceError:
            return "lost"

    outcomes = await asyncio.gather(reply("24h"), reply("7d"))
    assert sorted(outcomes) == ["lost", "won"]
    async with database.session() as session:
        stored, _ = await create_clarification(
            session, service_account_id=ids["account_id"], request=request
        )
        assert stored.status == ClarificationStatus.ANSWERED.value
        assert stored.answer_raw in {"24h", "7d"}


@pytest.mark.asyncio
async def test_clarification_reply_is_scoped_to_telegram_chat(database: Database) -> None:
    ids = await seed_scope(database)
    request = AskUserInput(
        project_id=ids["project_id"],
        agent_run_id="run-chat-scope",
        correlation_id="corr-chat-scope",
        idempotency_key="idem-chat-scope",
        recipient_user_id=ids["user_id"],
        context="Need a scoped reply",
        question="Which chat is this?",
        expires_at=utcnow() + timedelta(hours=1),
    )
    async with database.session() as session:
        clarification, _ = await create_clarification(
            session,
            service_account_id=ids["account_id"],
            request=request,
        )
        clarification.telegram_chat_id = 9001
        clarification.telegram_message_id = 44

    async with database.session() as session:
        with pytest.raises(ServiceError) as error:
            await answer_clarification_from_telegram(
                session,
                telegram_user_id=9001,
                telegram_chat_id=8001,
                reply_to_message_id=44,
                answer="wrong chat",
            )
    assert error.value.code == "request_not_found"


@pytest.mark.asyncio
async def test_cancelled_and_expired_clarifications_are_not_delivered(
    database: Database,
) -> None:
    ids = await seed_scope(database)
    base = AskUserInput(
        project_id=ids["project_id"],
        agent_run_id="run-delivery-state",
        correlation_id="corr-delivery-state",
        idempotency_key="idem-delivery-cancelled",
        recipient_user_id=ids["user_id"],
        context="Delivery state",
        question="Should this be sent?",
        expires_at=utcnow() + timedelta(hours=1),
    )
    async with database.session() as session:
        cancelled, _ = await create_clarification(
            session,
            service_account_id=ids["account_id"],
            request=base,
        )
        expired, _ = await create_clarification(
            session,
            service_account_id=ids["account_id"],
            request=base.model_copy(
                update={
                    "idempotency_key": "idem-delivery-expired",
                    "correlation_id": "corr-delivery-expired",
                }
            ),
        )
    async with database.session() as session:
        await cancel_clarification(
            session,
            service_account_id=ids["account_id"],
            request_id=cancelled.id,
            reason="No longer needed",
        )
        await session.execute(
            update(Clarification)
            .where(Clarification.id == expired.id)
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )

    adapter = TelegramAdapter(
        Settings(database_url=os.environ["DCA_TEST_DATABASE_URL"]),
        database,
    )
    adapter.bot.send_message = AsyncMock()  # type: ignore[method-assign]
    try:
        assert await adapter.deliver_clarification(cancelled.id) is False
        assert await adapter.deliver_clarification(expired.id) is False
        adapter.bot.send_message.assert_not_awaited()
    finally:
        await adapter.close()

    async with database.session() as session:
        stored_expired = await session.get(Clarification, expired.id)
        assert stored_expired is not None
        assert stored_expired.status == ClarificationStatus.EXPIRED.value


@pytest.mark.asyncio
async def test_delivery_serializes_with_cancel_and_preserves_notification(
    database: Database,
) -> None:
    ids = await seed_scope(database)
    request = AskUserInput(
        project_id=ids["project_id"],
        agent_run_id="run-delivery-race",
        correlation_id="corr-delivery-race",
        idempotency_key="idem-delivery-race",
        recipient_user_id=ids["user_id"],
        context="Delivery race",
        question="Can this race with cancel?",
        expires_at=utcnow() + timedelta(hours=1),
    )
    async with database.session() as session:
        clarification, _ = await create_clarification(
            session,
            service_account_id=ids["account_id"],
            request=request,
        )

    entered_send = asyncio.Event()
    release_send = asyncio.Event()

    async def delayed_send(**_: Any) -> SimpleNamespace:
        entered_send.set()
        await release_send.wait()
        return SimpleNamespace(chat=SimpleNamespace(id=9001), message_id=55)

    async def cancel() -> Clarification:
        async with database.session() as session:
            return await cancel_clarification(
                session,
                service_account_id=ids["account_id"],
                request_id=clarification.id,
                reason="Concurrent cancel",
            )

    adapter = TelegramAdapter(
        Settings(database_url=os.environ["DCA_TEST_DATABASE_URL"]),
        database,
    )
    adapter.bot.send_message = AsyncMock(side_effect=delayed_send)  # type: ignore[method-assign]
    delivery_task = asyncio.create_task(adapter.deliver_clarification(clarification.id))
    await asyncio.wait_for(entered_send.wait(), timeout=2)
    cancel_task = asyncio.create_task(cancel())
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(cancel_task), timeout=0.1)
        release_send.set()
        assert await asyncio.wait_for(delivery_task, timeout=2) is True
        cancelled = await asyncio.wait_for(cancel_task, timeout=2)
        assert cancelled.status == ClarificationStatus.CANCELLED.value
    finally:
        release_send.set()
        await adapter.close()

    async with database.session() as session:
        stored = await session.get(Clarification, clarification.id)
        assert stored is not None
        assert stored.telegram_message_id == 55
        assert stored.status == ClarificationStatus.CANCELLED.value
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.kind == "telegram.notify_clarification_cancelled")
            )
            == 1
        )


@pytest.mark.asyncio
async def test_cross_project_scope_is_denied(database: Database) -> None:
    ids = await seed_scope(database)
    async with database.session() as session:
        with pytest.raises(ServiceError) as error:
            await require_service_scope(
                session,
                service_account_id=ids["account_id"],
                project_id=ids["other_project_id"],
                tool="telegram.ask_user",
            )
    assert error.value.code == "forbidden"


@pytest.mark.asyncio
async def test_telegram_update_is_queued_once(database: Database) -> None:
    payload = {"update_id": 777, "message": {"message_id": 1}}
    async with database.session() as session:
        assert await reserve_telegram_update(session, 777, payload) is True
        await queue_telegram_update(session, 777, payload)
    async with database.session() as session:
        assert await reserve_telegram_update(session, 777, payload) is False
        assert await session.scalar(select(func.count()).select_from(Job)) == 1


@pytest.mark.asyncio
async def test_telegram_update_reservation_rolls_back_when_enqueue_fails(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_enqueue(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic enqueue failure")

    monkeypatch.setattr("dca.telegram.enqueue_job", fail_enqueue)
    payload = {"update_id": 778, "message": {"message_id": 1}}

    with pytest.raises(RuntimeError, match="synthetic enqueue failure"):
        async with database.session() as session:
            assert await reserve_telegram_update(session, 778, payload) is True
            await queue_telegram_update(session, 778, payload)

    async with database.session() as session:
        assert await session.get(TelegramUpdate, 778) is None
        assert await session.scalar(select(func.count()).select_from(Job)) == 0


@pytest.mark.asyncio
async def test_concurrent_repository_sync_reuses_active_job(
    database: Database, tmp_path: Path
) -> None:
    async with database.session() as session:
        project = Project(slug=f"sync-{uuid4().hex[:8]}", name="Sync")
        session.add(project)
        await session.flush()
        repository = Repository(
            project_id=project.id,
            name="backend",
            ssh_url="git@example.invalid:backend.git",
        )
        session.add(repository)
        await session.flush()
        repository_id = repository.id

    secret = "integration-session-secret-32-bytes"  # noqa: S105 - synthetic credential
    access_key = uuid4()
    async with database.session() as session:
        principal = AdminPrincipal(name=f"integration-admin-{uuid4().hex[:8]}")
        session.add(principal)
        await session.flush()
        admin_key = AdminAccessKey(
            principal_id=principal.id,
            fingerprint=admin_key_fingerprint(access_key, secret),
        )
        session.add(admin_key)
        await session.flush()
        admin_session = AdminSession(
            access_key_id=admin_key.id,
            expires_at=utcnow() + timedelta(days=1),
        )
        session.add(admin_session)
        await session.flush()
        admin_session_id = admin_session.id
    app = create_app(
        Settings(
            public_url="http://testserver",
            database_url=os.environ["DCA_TEST_DATABASE_URL"],
            session_secret=SecretStr(secret),
            cookie_secure=False,
            repository_root=tmp_path / "repositories",
            snapshot_root=tmp_path / "snapshots",
        )
    )
    cookie = URLSafeTimedSerializer(secret, salt="dca-admin-session-v2").dumps(
        {"session_id": str(admin_session_id)}
    )
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
            cookies={"dca_admin": cookie},
        ) as client:
            responses = await asyncio.gather(
                client.post(
                    f"/api/v1/repositories/{repository_id}/sync",
                    headers={"Origin": "http://testserver"},
                ),
                client.post(
                    f"/api/v1/repositories/{repository_id}/sync",
                    headers={"Origin": "http://testserver"},
                ),
            )
        assert [response.status_code for response in responses] == [202, 202]
        assert responses[0].json()["job_id"] == responses[1].json()["job_id"]
        async with database.session() as session:
            active_jobs = list(
                await session.scalars(
                    select(Job).where(
                        Job.kind == "repository.sync",
                        Job.status.in_(
                            (
                                JobStatus.QUEUED.value,
                                JobStatus.RUNNING.value,
                                JobStatus.RETRY.value,
                            )
                        ),
                        Job.payload["repository_id"].as_string() == str(repository_id),
                    )
                )
            )
            audit_count = await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.event_type == "repository.sync_requested",
                    AuditEvent.subject_id == str(repository_id),
                )
            )
        assert len(active_jobs) == 1
        assert audit_count == 1
        assert active_jobs[0].payload["generation"] == 1
    finally:
        await app.state.telegram.close()
        await app.state.redis.aclose()
        await app.state.database.close()


@pytest.mark.asyncio
async def test_signed_github_push_is_deduplicated_and_marks_repository_stale(
    database: Database, tmp_path: Path
) -> None:
    async with database.session() as session:
        project = Project(slug=f"webhook-{uuid4().hex[:8]}", name="Webhook")
        second_project = Project(slug=f"webhook-{uuid4().hex[:8]}", name="Webhook 2")
        disabled_project = Project(slug=f"webhook-{uuid4().hex[:8]}", name="Webhook off")
        session.add_all([project, second_project, disabled_project])
        await session.flush()
        repository = Repository(
            project_id=project.id,
            name="backend_ai",
            ssh_url="git@github.com:Matrena-VPN/backend_ai.git",
            github_repository="matrena-vpn/backend_ai",
            auto_sync_enabled=True,
            status="ready",
            current_commit="1" * 40,
        )
        second_repository = Repository(
            project_id=second_project.id,
            name="backend_ai",
            ssh_url="git@github.com:Matrena-VPN/backend_ai.git",
            github_repository="matrena-vpn/backend_ai",
            auto_sync_enabled=True,
            status="ready",
            current_commit="1" * 40,
        )
        disabled_repository = Repository(
            project_id=disabled_project.id,
            name="backend_ai_disabled",
            ssh_url="git@github.com:Matrena-VPN/backend_ai.git",
            github_repository="matrena-vpn/backend_ai",
            auto_sync_enabled=True,
            status=RepositoryStatus.DISABLED.value,
            current_commit="1" * 40,
        )
        session.add_all([repository, second_repository, disabled_repository])
        await session.flush()
        repository_id = repository.id
        second_repository_id = second_repository.id
        disabled_repository_id = disabled_repository.id

    secret = "g" * 32
    commit = "2" * 40
    body = orjson.dumps(
        {
            "ref": "refs/heads/main",
            "after": commit,
            "deleted": False,
            "repository": {"full_name": "Matrena-VPN/backend_ai"},
        }
    )
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "X-Hub-Signature-256": signature,
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": str(uuid4()),
        "Content-Type": "application/json",
    }
    app = create_app(
        Settings(
            public_url="http://testserver",
            database_url=os.environ["DCA_TEST_DATABASE_URL"],
            session_secret=SecretStr("integration-session-secret-32-bytes"),
            cookie_secure=False,
            github_webhook_secret=SecretStr(secret),
            repository_root=tmp_path / "repositories",
            snapshot_root=tmp_path / "snapshots",
        )
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            first = await client.post("/webhooks/github", content=body, headers=headers)
            duplicate = await client.post("/webhooks/github", content=body, headers=headers)
            replay_headers = {**headers, "X-GitHub-Delivery": str(uuid4())}
            replay = await client.post(
                "/webhooks/github", content=body, headers=replay_headers
            )

        assert first.status_code == 202
        assert first.json()["queued"] is True
        assert len(first.json()["jobs"]) == 2
        assert duplicate.status_code == 202
        assert duplicate.json()["queued"] is False
        assert {row["job_id"] for row in duplicate.json()["jobs"]} == {
            row["job_id"] for row in first.json()["jobs"]
        }
        assert replay.status_code == 202
        assert replay.json()["queued"] is False
        assert {row["job_id"] for row in replay.json()["jobs"]} == {
            row["job_id"] for row in first.json()["jobs"]
        }
        async with database.session() as session:
            stored = await session.get(Repository, repository_id)
            second_stored = await session.get(Repository, second_repository_id)
            disabled_stored = await session.get(Repository, disabled_repository_id)
            assert stored is not None
            assert second_stored is not None
            assert disabled_stored is not None
            assert stored.status == "stale"
            assert second_stored.status == "stale"
            assert stored.sync_generation == 1
            assert second_stored.sync_generation == 1
            assert stored.last_webhook_commit == commit
            assert second_stored.last_webhook_commit == commit
            assert disabled_stored.status == RepositoryStatus.DISABLED.value
            assert disabled_stored.sync_generation == 0
            assert disabled_stored.last_webhook_commit is None
            jobs = list(
                await session.scalars(
                    select(Job).where(Job.kind == "repository.sync")
                )
            )
            assert len(jobs) == 2
            assert {job.payload["repository_id"] for job in jobs} == {
                str(repository_id),
                str(second_repository_id),
            }
            assert {job.payload["requested_commit"] for job in jobs} == {commit}
    finally:
        await app.state.telegram.close()
        await app.state.redis.aclose()
        await app.state.database.close()


@pytest.mark.asyncio
async def test_repository_reconcile_queues_one_fallback_sync(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_now = datetime(2026, 7, 22, 6, 0, tzinfo=UTC)
    monkeypatch.setattr(worker_module, "utcnow", lambda: fixed_now)
    async with database.session() as session:
        project = Project(slug=f"reconcile-{uuid4().hex[:8]}", name="Reconcile")
        session.add(project)
        await session.flush()
        repository = Repository(
            project_id=project.id,
            name="backend_ai",
            ssh_url="git@github.com:Matrena-VPN/backend_ai.git",
            github_repository="matrena-vpn/backend_ai",
            auto_sync_enabled=True,
        )
        session.add(repository)
        await session.flush()
        repository_id = repository.id

    worker = Worker.__new__(Worker)
    worker.settings = SimpleNamespace(repository_reconcile_seconds=300)
    worker.database = database
    worker._last_repository_reconcile = -1_000_000_000.0

    await worker._reconcile_repositories_if_due()

    async with database.session() as session:
        job = await session.scalar(
            select(Job).where(
                Job.kind == "repository.sync",
                Job.payload["repository_id"].as_string() == str(repository_id),
            )
        )
        assert job is not None
        job.status = JobStatus.FAILED.value

    restarted_worker = Worker.__new__(Worker)
    restarted_worker.settings = SimpleNamespace(repository_reconcile_seconds=300)
    restarted_worker.database = database
    restarted_worker._last_repository_reconcile = -1_000_000_000.0
    await restarted_worker._reconcile_repositories_if_due()

    async with database.session() as session:
        jobs = list(
            await session.scalars(
                select(Job).where(
                    Job.kind == "repository.sync",
                    Job.payload["repository_id"].as_string() == str(repository_id),
                )
            )
        )
    assert len(jobs) == 1
    assert jobs[0].payload["source"] == "reconcile"
    assert jobs[0].payload["generation"] == 1
    async with database.session() as session:
        repository = await session.get(Repository, repository_id)
        assert repository is not None
        assert repository.sync_generation == 1


@pytest.mark.asyncio
async def test_newer_sync_generation_wins_across_two_workers(
    database: Database, tmp_path: Path
) -> None:
    async with database.session() as session:
        project = Project(slug=f"sync-order-{uuid4().hex[:8]}", name="Sync order")
        session.add(project)
        await session.flush()
        repository = Repository(
            project_id=project.id,
            name="backend_ai",
            ssh_url="git@github.com:Matrena-VPN/backend_ai.git",
            status="ready",
            current_commit="0" * 40,
        )
        session.add(repository)
        await session.flush()
        first_job, created = await enqueue_repository_sync(
            session,
            repository=repository,
            source="github",
            requested_commit="1" * 40,
            deduplication_key=f"test:first:{repository.id}",
        )
        assert created is True
        repository_id = repository.id
        first_generation = first_job.payload["generation"]

    class OrderedSnapshots:
        def __init__(self) -> None:
            self.calls = 0
            self.active = 0
            self.max_active = 0
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()

        async def sync(self, _: Repository) -> str:
            self.calls += 1
            call = self.calls
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                if call == 1:
                    self.first_started.set()
                    await self.release_first.wait()
                return str(call) * 40
            finally:
                self.active -= 1

        async def materialize(self, _: Repository, __: str) -> Path:
            return tmp_path

    snapshots = OrderedSnapshots()
    old_worker = Worker.__new__(Worker)
    old_worker.database = database
    old_worker.snapshots = snapshots
    new_worker = Worker.__new__(Worker)
    new_worker.database = database
    new_worker.snapshots = snapshots

    old_task = asyncio.create_task(
        old_worker._sync_repository(
            repository_id,
            generation=first_generation,
            requested_commit="1" * 40,
            source="github",
        )
    )
    await asyncio.wait_for(snapshots.first_started.wait(), timeout=2)

    async with database.session() as session:
        repository = await session.scalar(
            select(Repository).where(Repository.id == repository_id).with_for_update()
        )
        assert repository is not None
        second_job, created = await enqueue_repository_sync(
            session,
            repository=repository,
            source="github",
            requested_commit="2" * 40,
            deduplication_key=f"test:second:{repository.id}",
        )
        assert created is True
        second_generation = second_job.payload["generation"]

    with pytest.raises(ServiceError) as busy:
        await asyncio.wait_for(
            new_worker._sync_repository(
                repository_id,
                generation=second_generation,
                requested_commit="2" * 40,
                source="github",
            ),
            timeout=2,
        )
    assert busy.value.code == "repository_sync_busy"
    assert busy.value.retryable is True
    assert snapshots.calls == 1

    snapshots.release_first.set()
    old_result = await old_task
    new_result = await new_worker._sync_repository(
        repository_id,
        generation=second_generation,
        requested_commit="2" * 40,
        source="github",
    )

    assert old_result["superseded"] is True
    assert old_result["current_generation"] == second_generation
    assert new_result["commit"] == "2" * 40
    assert snapshots.max_active == 1
    async with database.session() as session:
        repository = await session.get(Repository, repository_id)
        assert repository is not None
        assert repository.status == "ready"
        assert repository.current_commit == "2" * 40
        assert repository.sync_generation == second_generation
        superseded = await session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.event_type == "repository.sync_superseded",
                AuditEvent.subject_id == str(repository_id),
            )
        )
        assert superseded == 1
