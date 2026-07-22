import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, call
from uuid import uuid4

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramConflictError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.methods import GetUpdates, SendMessage
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

import dca.worker as worker_module
from dca.claude import ClaudeError, compile_agent_policy
from dca.config import Settings
from dca.db import AgentMessage, ConversationThread, Interaction, Job, Repository, TelegramChat
from dca.domain import KnowledgeAnswer
from dca.memory import ConversationContext, ConversationContextMessage
from dca.service import ServiceError
from dca.worker import (
    TELEGRAM_EXTERNAL_ACTIONS,
    Worker,
    conversation_message_record,
    conversation_prompt_context,
    interaction_agent_role,
    normalize_guard_reply,
    render_answer,
    sanitize_stream_text,
    trusted_requester_profile,
)


def test_telegram_mode_is_strict_and_webhook_compatible() -> None:
    assert Settings(telegram_mode="polling").telegram_mode == "polling"
    assert Settings(telegram_mode="webhook").telegram_mode == "webhook"
    with pytest.raises(ValidationError):
        Settings(telegram_mode="updates")  # type: ignore[arg-type]


def test_knowledge_concurrency_defaults_to_five_and_uses_env(monkeypatch) -> None:
    assert Settings().knowledge_concurrency == 5
    monkeypatch.setenv("DCA_KNOWLEDGE_CONCURRENCY", "3")
    assert Settings().knowledge_concurrency == 3
    with pytest.raises(ValidationError):
        Settings(knowledge_concurrency=6)


def test_worker_entrypoint_treats_sigint_as_clean_shutdown(monkeypatch) -> None:
    def interrupted(coroutine: Any) -> None:
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(worker_module.asyncio, "run", interrupted)

    worker_module.run()


def test_interaction_agent_role_is_server_marker_only() -> None:
    guarded = SimpleNamespace(source_ref={"agent_role": "bydlo_guard"})
    ordinary = SimpleNamespace(source_ref={"agent_role": "admin", "guard_kinds": ["token"]})

    assert interaction_agent_role(guarded) == "bydlo_guard"
    assert interaction_agent_role(ordinary) == "knowledge"


def test_render_answer_keeps_verification_sources_internal() -> None:
    rendered = render_answer(
        answer_markdown="Используйте `GET /avatars`.",
        uncertainty=[],
    )

    assert rendered == "Используйте `GET /avatars`."
    assert "Источники" not in rendered
    assert "src/" not in rendered


def test_guard_reply_keeps_generated_words_but_forces_plain_compact_text() -> None:
    normalized = normalize_guard_reply(
        "## Отказ\n\n- **ХУЙ ТЕБЕ**, а не ключ.\n- Попытка записана в аудит."
    )

    assert normalized == "Отказ ХУЙ ТЕБЕ, а не ключ. Попытка записана в аудит."
    assert "\n" not in normalized
    assert "**" not in normalized


def test_long_answer_memory_record_preserves_both_ends_within_database_limit() -> None:
    answer = "A" * 40_000 + "TAIL"

    record = conversation_message_record(answer)

    assert len(record) == 32_000
    assert record.startswith("A" * 100)
    assert record.endswith("TAIL")
    assert "Полный ответ сохранён в interaction" in record


@pytest.mark.asyncio
async def test_private_draft_heartbeat_refreshes_until_answer_is_durable(monkeypatch) -> None:
    refresh = AsyncMock(side_effect=[False, False, True])
    monkeypatch.setattr(worker_module.asyncio, "sleep", AsyncMock())
    worker = Worker.__new__(Worker)
    interaction = SimpleNamespace(
        source="telegram",
        source_ref={"delivery": {"kind": "private_draft"}},
    )

    await worker._draft_heartbeat(interaction, refresh=refresh)  # type: ignore[arg-type]

    assert refresh.await_count == 3


def test_prompt_context_keeps_legitimate_latest_historical_user_message() -> None:
    message = ConversationContextMessage(
        role="user",
        source="telegram",
        content="Предыдущее решение",
        author_user_id=uuid4(),
        created_at=datetime.now(UTC),
    )
    context = ConversationContext(
        thread_id=uuid4(),
        summary=None,
        facts=(),
        messages=(message,),
    )

    payload = conversation_prompt_context(context)

    assert payload["messages"][0]["content"] == "Предыдущее решение"


def test_stream_privacy_holds_unfinished_secret_tokens() -> None:
    partial, findings = sanitize_stream_text(
        "Проверяю sk-ant-short",
        level="strict",
        location="thinking",
    )
    private_key, key_findings = sanitize_stream_text(
        "До ключа -----BEGIN PRIVATE KEY-----\nsecret-body",
        level="strict",
        location="thinking",
    )
    metadata, metadata_findings = sanitize_stream_text(
        "Файл /etc/dca/dca.env содержит DCA_SESSION_SEC",
        level="strict",
        location="answer",
    )

    assert partial == "Проверяю"
    assert "sk-ant" not in partial
    assert findings == []
    assert private_key == "До ключа [REDACTED:private_key]"
    assert key_findings[0]["kind"] == "private_key"
    assert "/etc/dca" not in metadata
    assert "DCA_SESSION_SEC" not in metadata
    assert {finding["kind"] for finding in metadata_findings} == {
        "environment_metadata",
        "internal_server_path",
    }


@pytest.mark.asyncio
async def test_poll_batch_ingests_each_update_before_advancing_offset(monkeypatch) -> None:
    worker = Worker.__new__(Worker)
    payloads = [
        {"update_id": 41, "message": {"message_id": 1}},
        {"update_id": 42, "message": {"message_id": 2}},
    ]

    class FakeUpdate:
        def __init__(self, payload: dict[str, Any]) -> None:
            self.update_id = int(payload["update_id"])
            self.payload = payload

        def model_dump(self, **_: Any) -> dict[str, Any]:
            return dict(self.payload)

    bot = SimpleNamespace(get_updates=AsyncMock(return_value=list(map(FakeUpdate, payloads))))
    worker.telegram = SimpleNamespace(bot=bot, allowed_updates=lambda: ["message"])

    @asynccontextmanager
    async def session():
        yield object()

    worker.database = SimpleNamespace(session=session)
    ingest = AsyncMock()
    monkeypatch.setattr(worker_module, "ingest_telegram_update", ingest)

    assert await worker._poll_telegram_batch(None) == 43
    bot.get_updates.assert_awaited_once_with(
        offset=None,
        timeout=worker_module.TELEGRAM_POLL_TIMEOUT_SECONDS,
        allowed_updates=["message"],
        request_timeout=worker_module.TELEGRAM_POLL_REQUEST_TIMEOUT_SECONDS,
    )
    assert [call.args[2]["update_id"] for call in ingest.await_args_list] == [41, 42]
    assert all(call.kwargs["actor_id"] == "polling-worker" for call in ingest.await_args_list)


@pytest.mark.asyncio
async def test_worker_runs_polling_and_jobs_concurrently() -> None:
    worker = Worker.__new__(Worker)
    worker.settings = SimpleNamespace(telegram_mode="polling", knowledge_concurrency=3)
    worker.recover_stale_jobs = AsyncMock()
    worker._run_job_loop = AsyncMock()
    worker._poll_telegram_forever = AsyncMock()
    worker.telegram = SimpleNamespace(close=AsyncMock())
    worker.database = SimpleNamespace(close=AsyncMock())
    worker.worker_id = "test-worker"

    await worker.run_forever()

    assert worker._run_job_loop.await_count == 4
    assert worker._run_job_loop.await_args_list.count(call(exclude_kind="knowledge.answer")) == 1
    assert worker._run_job_loop.await_args_list.count(call(only_kind="knowledge.answer")) == 3
    worker._poll_telegram_forever.assert_awaited_once_with()
    worker.telegram.close.assert_awaited_once_with()
    worker.database.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_claim_job_filters_knowledge_pool_with_skip_locked() -> None:
    statements = []

    class FakeSession:
        async def scalar(self, statement):
            statements.append(statement)
            return None

    @asynccontextmanager
    async def session():
        yield FakeSession()

    worker = Worker.__new__(Worker)
    worker.database = SimpleNamespace(session=session)

    assert await worker.claim_job(only_kind="knowledge.answer") is None
    assert await worker.claim_job(exclude_kind="knowledge.answer") is None

    only = statements[0].compile(dialect=postgresql.dialect())
    exclude = statements[1].compile(dialect=postgresql.dialect())
    assert "FOR UPDATE SKIP LOCKED" in str(only)
    assert "jobs.kind =" in str(only)
    assert only.params["kind_1"] == "knowledge.answer"
    assert "jobs.kind !=" in str(exclude)
    assert exclude.params["kind_1"] == "knowledge.answer"


@pytest.mark.asyncio
async def test_polling_retries_network_errors_with_backoff(monkeypatch) -> None:
    worker = Worker.__new__(Worker)
    network_error = TelegramNetworkError(method=GetUpdates(), message="proxy unavailable")
    worker._poll_telegram_batch = AsyncMock(
        side_effect=[network_error, None, network_error, asyncio.CancelledError()]
    )
    sleep = AsyncMock()
    jitter = iter([0.2, 0.3])
    monkeypatch.setattr(worker_module.asyncio, "sleep", sleep)
    monkeypatch.setattr(worker_module.random, "uniform", lambda *_: next(jitter))

    with pytest.raises(asyncio.CancelledError):
        await worker._poll_telegram_loop()

    assert [call.args for call in sleep.await_args_list] == [(1.2,), (1.3,)]
    assert all(call.args == (None,) for call in worker._poll_telegram_batch.await_args_list)


@pytest.mark.asyncio
async def test_polling_honors_full_telegram_retry_after(monkeypatch) -> None:
    worker = Worker.__new__(Worker)
    rate_limit = TelegramRetryAfter(
        method=GetUpdates(),
        message="rate limited",
        retry_after=75,
    )
    worker._poll_telegram_batch = AsyncMock(side_effect=[rate_limit, asyncio.CancelledError()])
    sleep = AsyncMock()
    monkeypatch.setattr(worker_module.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await worker._poll_telegram_loop()

    sleep.assert_awaited_once_with(75.0)


@pytest.mark.parametrize(
    "error_type",
    [
        TelegramConflictError,
        TelegramUnauthorizedError,
        TelegramForbiddenError,
        TelegramBadRequest,
    ],
)
@pytest.mark.asyncio
async def test_polling_fails_fast_on_non_retryable_telegram_errors(
    monkeypatch,
    error_type: type[Exception],
) -> None:
    worker = Worker.__new__(Worker)
    error = error_type(method=GetUpdates(), message="invalid polling configuration")
    worker._poll_telegram_batch = AsyncMock(side_effect=error)
    sleep = AsyncMock()
    monkeypatch.setattr(worker_module.asyncio, "sleep", sleep)

    with pytest.raises(error_type):
        await worker._poll_telegram_loop()

    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_polling_fails_fast_on_unexpected_programming_error(monkeypatch) -> None:
    worker = Worker.__new__(Worker)
    worker._poll_telegram_batch = AsyncMock(side_effect=TypeError("invalid update contract"))
    sleep = AsyncMock()
    monkeypatch.setattr(worker_module.asyncio, "sleep", sleep)

    with pytest.raises(TypeError, match="invalid update contract"):
        await worker._poll_telegram_loop()

    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_polling_advisory_lock_is_released() -> None:
    worker = Worker.__new__(Worker)
    connection = SimpleNamespace(scalar=AsyncMock(side_effect=[True, True]))

    @asynccontextmanager
    async def connect():
        yield connection

    worker.database = SimpleNamespace(engine=SimpleNamespace(connect=connect))

    with pytest.raises(RuntimeError, match="test cancellation"):
        async with worker._telegram_poll_lock():
            raise RuntimeError("test cancellation")

    assert connection.scalar.await_count == 2
    assert "pg_try_advisory_lock" in str(connection.scalar.await_args_list[0].args[0])
    assert "pg_advisory_unlock" in str(connection.scalar.await_args_list[1].args[0])


@pytest.mark.asyncio
async def test_polling_refuses_second_consumer() -> None:
    worker = Worker.__new__(Worker)
    connection = SimpleNamespace(scalar=AsyncMock(return_value=False))

    @asynccontextmanager
    async def connect():
        yield connection

    worker.database = SimpleNamespace(engine=SimpleNamespace(connect=connect))

    with pytest.raises(RuntimeError, match="already holds the lock"):
        async with worker._telegram_poll_lock():
            pass


@pytest.mark.asyncio
async def test_repository_sync_lock_refuses_busy_repository_without_waiting() -> None:
    worker = Worker.__new__(Worker)
    connection = SimpleNamespace(
        scalar=AsyncMock(return_value=False),
        invalidate=AsyncMock(),
    )

    @asynccontextmanager
    async def connect():
        yield connection

    worker.database = SimpleNamespace(engine=SimpleNamespace(connect=connect))

    with pytest.raises(ServiceError) as error:
        async with worker._repository_sync_lock(uuid4()):
            pass

    assert error.value.code == "repository_sync_busy"
    assert error.value.retryable is True
    assert "pg_try_advisory_lock" in str(connection.scalar.await_args.args[0])
    connection.invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_conversation_context_lock_refuses_busy_thread_without_waiting() -> None:
    worker = Worker.__new__(Worker)
    connection = SimpleNamespace(
        scalar=AsyncMock(return_value=False),
        invalidate=AsyncMock(),
    )

    @asynccontextmanager
    async def connect():
        yield connection

    worker.database = SimpleNamespace(engine=SimpleNamespace(connect=connect))

    with pytest.raises(ServiceError) as error:
        async with worker._conversation_context_lock(uuid4()):
            pass

    assert error.value.code == "conversation_context_busy"
    assert error.value.retryable is True
    assert "pg_try_advisory_lock" in str(connection.scalar.await_args.args[0])
    connection.invalidate.assert_not_awaited()


@pytest.mark.parametrize(
    "unlock_result",
    [False, RuntimeError("connection lost during unlock")],
    ids=["unlock-returned-false", "unlock-raised"],
)
@pytest.mark.asyncio
async def test_repository_sync_lock_discards_connection_when_unlock_is_uncertain(
    unlock_result: bool | Exception,
) -> None:
    worker = Worker.__new__(Worker)
    connection = SimpleNamespace(
        scalar=AsyncMock(side_effect=[True, unlock_result]),
        invalidate=AsyncMock(),
    )

    @asynccontextmanager
    async def connect():
        yield connection

    worker.database = SimpleNamespace(engine=SimpleNamespace(connect=connect))

    async with worker._repository_sync_lock(uuid4()):
        pass

    if isinstance(unlock_result, Exception):
        connection.invalidate.assert_awaited_once_with(unlock_result)
    else:
        connection.invalidate.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_stale_repository_sync_skips_advisory_lock(monkeypatch) -> None:
    repository = Repository(
        id=uuid4(),
        project_id=uuid4(),
        name="backend_ai",
        ssh_url="git@github.com:Matrena-VPN/backend_ai.git",
        status="syncing",
        sync_generation=2,
    )
    session_object = SimpleNamespace(scalar=AsyncMock(return_value=repository))

    @asynccontextmanager
    async def session():
        yield session_object

    @asynccontextmanager
    async def unexpected_lock(_: Any):
        raise AssertionError("stale generation must not contend on the advisory lock")
        yield

    worker = Worker.__new__(Worker)
    worker.database = SimpleNamespace(session=session)
    worker.snapshots = SimpleNamespace(sync=AsyncMock(), materialize=AsyncMock())
    worker._repository_sync_lock = unexpected_lock
    monkeypatch.setattr(worker_module, "append_audit", AsyncMock())

    result = await worker._sync_repository(repository.id, generation=1, source="github")

    assert result["superseded"] is True
    assert result["current_generation"] == 2
    assert repository.status == "syncing"
    worker.snapshots.sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_repository_sync_skips_advisory_lock() -> None:
    repository = Repository(
        id=uuid4(),
        project_id=uuid4(),
        name="backend_ai",
        ssh_url="git@github.com:Matrena-VPN/backend_ai.git",
        status="disabled",
        sync_generation=1,
    )
    session_object = SimpleNamespace(scalar=AsyncMock(return_value=repository))

    @asynccontextmanager
    async def session():
        yield session_object

    @asynccontextmanager
    async def unexpected_lock(_: Any):
        raise AssertionError("disabled repository must not acquire the advisory lock")
        yield

    worker = Worker.__new__(Worker)
    worker.database = SimpleNamespace(session=session)
    worker.snapshots = SimpleNamespace(sync=AsyncMock(), materialize=AsyncMock())
    worker._repository_sync_lock = unexpected_lock

    with pytest.raises(ServiceError, match="disabled") as error:
        await worker._sync_repository(repository.id, generation=1, source="admin")

    assert error.value.code == "repository_disabled"
    worker.snapshots.sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_repository_lock_contention_does_not_consume_job_attempt() -> None:
    worker = Worker.__new__(Worker)
    worker._dispatch = AsyncMock(
        side_effect=ServiceError(
            "repository_sync_busy",
            "Repository sync is already running",
            retryable=True,
        )
    )
    worker._retry = AsyncMock(return_value=True)
    job = Job(
        id=uuid4(),
        kind="repository.sync",
        payload={"repository_id": str(uuid4())},
        attempts=3,
        max_attempts=3,
    )

    await worker.process(job)

    worker._retry.assert_awaited_once_with(
        job,
        "repository_sync_busy",
        "Repository sync is already running",
        delay=2,
        consume_attempt=False,
    )


@pytest.mark.asyncio
async def test_conversation_lock_contention_does_not_consume_job_attempt() -> None:
    worker = Worker.__new__(Worker)
    worker._dispatch = AsyncMock(
        side_effect=ServiceError(
            "conversation_context_busy",
            "Another answer is already using this conversation context",
            retryable=True,
        )
    )
    worker._retry = AsyncMock(return_value=True)
    job = Job(
        id=uuid4(),
        kind="knowledge.answer",
        payload={"interaction_id": str(uuid4())},
        attempts=3,
        max_attempts=3,
    )

    await worker.process(job)

    worker._retry.assert_awaited_once_with(
        job,
        "conversation_context_busy",
        "Another answer is already using this conversation context",
        delay=1,
        consume_attempt=False,
    )


@pytest.mark.asyncio
async def test_polling_startup_deletes_webhook_and_keeps_backlog() -> None:
    worker = Worker.__new__(Worker)

    @asynccontextmanager
    async def lock():
        yield

    worker._telegram_poll_lock = lock
    worker.telegram = SimpleNamespace(
        bot=SimpleNamespace(delete_webhook=AsyncMock(return_value=True))
    )
    worker._poll_telegram_loop = AsyncMock()

    await worker._poll_telegram_forever()

    worker.telegram.bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
    worker._poll_telegram_loop.assert_awaited_once_with()


def test_trusted_requester_profile_whitelists_telegram_server_metadata() -> None:
    interaction = Interaction(
        source="telegram",
        source_ref={
            "telegram_user_id": 9001,
            "requester_profile": {
                "display_name": "Бека",
                "department": "Mobile",
                "stack": "Android / Kotlin",
                "unknown": "must not reach Claude",
                "role": {"invalid": "type"},
            },
        },
    )

    assert trusted_requester_profile(interaction) == {
        "display_name": "Бека",
        "department": "Mobile",
        "stack": "Android / Kotlin",
        "telegram_user_id": 9001,
    }
    interaction.source = "api"
    assert trusted_requester_profile(interaction) is None


@pytest.mark.asyncio
async def test_agent_message_delivery_revalidates_chat_and_records_message_id(monkeypatch) -> None:
    project_id = uuid4()
    chat_id = uuid4()
    message = AgentMessage(
        id=uuid4(),
        project_id=project_id,
        service_account_id=uuid4(),
        correlation_id="run-1",
        idempotency_key="message-1",
        target_user_id=None,
        target_chat_id=chat_id,
        text_markdown="Deployment finished",
        attachment_name=None,
        attachment_markdown=None,
        privacy_findings=[],
        status="queued",
    )
    chat = TelegramChat(
        id=chat_id,
        project_id=project_id,
        telegram_chat_id=-100,
        message_thread_id=7,
        kind="project_group",
        enabled=True,
    )

    class FakeSession:
        async def get(self, model, _key):
            return message if model is AgentMessage else chat if model is TelegramChat else None

        async def flush(self) -> None:
            return None

        def expunge(self, _value) -> None:
            return None

    @asynccontextmanager
    async def session():
        yield FakeSession()

    worker = Worker.__new__(Worker)
    worker.database = SimpleNamespace(session=session)
    worker.telegram = SimpleNamespace(deliver_agent_message=AsyncMock(return_value=73))
    monkeypatch.setattr(
        worker_module,
        "load_project_agent_settings",
        AsyncMock(
            return_value=SimpleNamespace(
                enabled=True,
                telegram_attach_markdown=True,
                privacy_level="strict",
            )
        ),
    )
    monkeypatch.setattr(worker_module, "append_audit", AsyncMock())
    enqueue_job_mock = AsyncMock()
    monkeypatch.setattr(worker_module, "enqueue_job", enqueue_job_mock)

    result = await worker._deliver_agent_message(message.id)

    assert result["status"] == "sent"
    assert message.status == "sent"
    assert message.telegram_message_id == 73
    worker.telegram.deliver_agent_message.assert_awaited_once_with(
        chat_id=-100,
        message_thread_id=7,
        text_markdown="Deployment finished",
        attachment_name=None,
        attachment_markdown=None,
    )
    enqueue_job_mock.assert_awaited_once()
    assert enqueue_job_mock.await_args.kwargs["kind"] == "conversation.remember_agent_message"
    assert enqueue_job_mock.await_args.kwargs["payload"] == {"agent_message_id": str(message.id)}


@pytest.mark.asyncio
async def test_agent_message_delivery_uncertainty_updates_durable_message(monkeypatch) -> None:
    session_object = SimpleNamespace(execute=AsyncMock())

    @asynccontextmanager
    async def session():
        yield session_object

    worker = Worker.__new__(Worker)
    worker.database = SimpleNamespace(session=session)
    monkeypatch.setattr(worker_module, "append_audit", AsyncMock())
    job = Job(
        id=uuid4(),
        kind="telegram.deliver_agent_message",
        payload={"agent_message_id": str(uuid4())},
    )

    await worker._delivery_uncertain(job, "telegram_network_timeout", "connection lost")

    assert session_object.execute.await_count == 2
    agent_update = session_object.execute.await_args_list[1].args[0]
    assert agent_update.compile().params["status"] == "delivery_uncertain"
    assert agent_update.compile().params["error_code"] == "telegram_network_timeout"


@pytest.mark.asyncio
async def test_final_retryable_model_failure_notifies_the_user(monkeypatch) -> None:
    worker = Worker.__new__(Worker)
    job = Job(
        id=uuid4(),
        kind="knowledge.answer",
        payload={"interaction_id": str(uuid4())},
        attempts=3,
        max_attempts=3,
    )

    async def fail_dispatch(_: Job) -> dict[str, Any]:
        raise ClaudeError("model_provider_timeout", "Timed out", retryable=True)

    retry = AsyncMock(return_value=False)
    publish_error = AsyncMock()
    monkeypatch.setattr(worker, "_dispatch", fail_dispatch)
    monkeypatch.setattr(worker, "_retry", retry)
    monkeypatch.setattr(worker, "_publish_interaction_error", publish_error)

    await worker.process(job)

    retry.assert_awaited_once_with(job, "model_provider_timeout", "Timed out")
    publish_error.assert_awaited_once_with(job, "model_provider_timeout")


@pytest.mark.asyncio
async def test_publish_interaction_error_keeps_claude_error_code() -> None:
    interaction = Interaction(id=uuid4(), status="running")

    class FakeSession:
        async def get(self, model, key):
            assert model is Interaction
            assert key == interaction.id
            return interaction

        async def flush(self) -> None:
            return None

        def expunge(self, value) -> None:
            assert value is interaction

    @asynccontextmanager
    async def session():
        yield FakeSession()

    worker = Worker.__new__(Worker)
    worker.database = SimpleNamespace(session=session)
    worker.telegram = SimpleNamespace(publish_knowledge_error=AsyncMock())
    job = Job(
        id=uuid4(),
        kind="knowledge.answer",
        payload={"interaction_id": str(interaction.id)},
    )

    await worker._publish_interaction_error(job, "model_provider_timeout")

    assert interaction.status == "failed"
    assert interaction.error_code == "model_provider_timeout"
    worker.telegram.publish_knowledge_error.assert_awaited_once_with(interaction)


@pytest.mark.asyncio
async def test_answer_streams_throttled_and_drops_unrequested_documents(monkeypatch) -> None:
    project_id = uuid4()
    repository = Repository(
        id=uuid4(),
        project_id=project_id,
        name="backend",
        ssh_url="git@github.com:example/backend.git",
    )
    interaction = Interaction(
        id=uuid4(),
        project_id=project_id,
        repository_id=repository.id,
        correlation_id="tg:test",
        source="telegram",
        source_ref={},
        question="Как подключить аватарки?",
        commit_sha="a" * 40,
        status="queued",
        conversation_thread_id=None,
    )
    settings = SimpleNamespace(
        enabled=True,
        memory_enabled=False,
        denied_globs=[],
        telegram_streaming_enabled=True,
        privacy_level="balanced",
        claude_model="sonnet",
        claude_effort="medium",
        base_prompt="",
        answer_style="normal",
    )

    class FakeSession:
        async def get(self, model, _key):
            if model is Interaction:
                return interaction
            if model is Repository:
                return repository
            return None

        async def flush(self) -> None:
            return None

        def expunge(self, _value) -> None:
            return None

    @asynccontextmanager
    async def session():
        yield FakeSession()

    heartbeat_started = asyncio.Event()
    heartbeat_stopped = asyncio.Event()

    async def heartbeat(
        _: Interaction,
        *,
        policy_guard: Callable[[], Awaitable[None]] | None = None,
        refresh: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        if policy_guard is not None:
            await policy_guard()
        heartbeat_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            heartbeat_stopped.set()

    answer = KnowledgeAnswer(
        answer_markdown="Используйте endpoint профиля.",
        artifacts=[],
        change_request={
            "kind": "integration",
            "title": "Добавить аватарки",
            "summary": "Claude ошибочно решил, что нужна backend-заявка.",
        },
        context_attestation={
            "contract_version": "dca-context-v1",
            "nonce": "1" * 32,
            "policy_sha256": "2" * 64,
            "context_sha256": "3" * 64,
        },
    )

    async def claude_answer(**kwargs):
        assert kwargs["tool_profile"] == "read_only"
        assert kwargs["artifact_requested"] is False
        on_stream = kwargs["on_stream"]
        assert on_stream is not None
        await heartbeat_started.wait()
        await on_stream(
            "Authorization: Bearer abcdefghijklmnop ",
            "Проверяю sk-ant-" + "a" * 24 + " ",
        )
        await on_stream("Используйте endpoint", "Проверяю API")
        return SimpleNamespace(
            answer=answer,
            accepted_citations=[],
            rejected_citations=[],
            cli_version="test",
            session_id=None,
            compaction_count=0,
            context_metadata={"contract_version": "dca-context-v1"},
        )

    worker = Worker.__new__(Worker)
    worker.settings = SimpleNamespace(session_secret=SimpleNamespace(get_secret_value=lambda: "x"))
    worker.database = SimpleNamespace(session=session)
    worker.snapshots = SimpleNamespace(materialize=AsyncMock(return_value=Path("/snapshot")))
    worker.claude = SimpleNamespace(answer=AsyncMock(side_effect=claude_answer))
    worker.telegram = SimpleNamespace(send_knowledge_stream=AsyncMock())
    worker._draft_heartbeat = heartbeat
    monkeypatch.setattr(
        worker_module,
        "load_project_agent_settings",
        AsyncMock(return_value=settings),
    )
    monkeypatch.setattr(worker_module, "load_system_secret", AsyncMock(return_value="oauth"))
    load_profile = AsyncMock(
        return_value={
            "role": "frontend",
            "stack": "JavaScript",
            "language": "ru",
            "knowledge_scope": "integration",
            "can_create_requests": True,
        }
    )
    monkeypatch.setattr(worker_module, "load_live_requester_profile", load_profile)
    create_request = AsyncMock(
        side_effect=ServiceError(
            "request_intent_required",
            "Explicit backend request intent is required",
        )
    )
    monkeypatch.setattr(worker_module, "create_agent_change_request", create_request)
    monkeypatch.setattr(worker_module, "enqueue_job", AsyncMock())
    monkeypatch.setattr(worker_module, "append_audit", AsyncMock())

    await worker._answer_interaction(interaction.id)

    assert heartbeat_stopped.is_set()
    worker.telegram.send_knowledge_stream.assert_awaited_once_with(
        interaction,
        answer_markdown="Authorization: [REDACTED:bearer_token]",
        thinking="Проверяю [REDACTED:anthropic_token]",
    )
    assert interaction.artifacts == []
    assert interaction.provider_metadata["document_requested"] is False
    assert interaction.provider_metadata["stream_privacy_findings"] == 2
    assert interaction.provider_metadata["proposal_suppressed"] is True
    assert load_profile.await_count == 4
    create_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_native_session_rotates_once_and_bootstraps_database_memory(
    monkeypatch,
) -> None:
    project_id = uuid4()
    repository = Repository(
        id=uuid4(),
        project_id=project_id,
        name="backend",
        ssh_url="git@github.com:example/backend.git",
        allowed_paths=["src"],
    )
    old_session_id = uuid4()
    thread = ConversationThread(
        id=uuid4(),
        project_id=project_id,
        user_id=uuid4(),
        chat_id=None,
        claude_session_id=old_session_id,
        claude_repository_id=repository.id,
        claude_commit_sha="a" * 40,
        claude_compaction_count=0,
    )
    interaction = Interaction(
        id=uuid4(),
        project_id=project_id,
        repository_id=repository.id,
        conversation_thread_id=thread.id,
        correlation_id="mcp:resume",
        source="mcp",
        source_ref={},
        question="Продолжай интеграцию",
        commit_sha="a" * 40,
        status="queued",
    )
    settings = SimpleNamespace(
        enabled=True,
        memory_enabled=True,
        memory_recent_messages=24,
        memory_max_context_chars=24_000,
        denied_globs=["tmp/**"],
        telegram_streaming_enabled=False,
        privacy_level="balanced",
        claude_model="sonnet",
        claude_effort="medium",
        base_prompt="",
        answer_style="normal",
    )
    thread.claude_policy_hash = compile_agent_policy(
        project_settings=settings,
        requester_profile=None,
        delivery_scope="external",
        repository_allowed_paths=repository.allowed_paths,
        repository_denied_globs=settings.denied_globs,
    ).policy_sha256

    class FakeSession:
        async def get(self, model, _key):
            if model is Interaction:
                return interaction
            if model is Repository:
                return repository
            if model is ConversationThread:
                return thread
            return None

        async def flush(self) -> None:
            return None

        def expunge(self, _value) -> None:
            return None

    @asynccontextmanager
    async def session():
        yield FakeSession()

    context = ConversationContext(
        thread_id=thread.id,
        summary="Ранее выбрали endpoint /avatars",
        facts=(),
        messages=(),
    )
    calls: list[dict[str, Any]] = []

    async def claude_answer(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise ClaudeError(
                "claude_session_unavailable",
                "Claude session is missing or expired",
                retryable=True,
            )
        return SimpleNamespace(
            answer=KnowledgeAnswer(
                answer_markdown="Используйте /avatars.",
                context_attestation={
                    "contract_version": "dca-context-v1",
                    "nonce": "1" * 32,
                    "policy_sha256": "2" * 64,
                    "context_sha256": "3" * 64,
                },
            ),
            accepted_citations=[],
            rejected_citations=[],
            cli_version="test",
            session_id=str(kwargs["session_id"]),
            compaction_count=0,
            context_metadata={"contract_version": "dca-context-v1"},
        )

    worker = Worker.__new__(Worker)
    worker.settings = SimpleNamespace(session_secret=SimpleNamespace(get_secret_value=lambda: "x"))
    worker.database = SimpleNamespace(session=session)
    worker.snapshots = SimpleNamespace(materialize=AsyncMock(return_value=Path("/snapshot")))
    worker.claude = SimpleNamespace(answer=AsyncMock(side_effect=claude_answer))
    worker.telegram = SimpleNamespace()
    monkeypatch.setattr(
        worker_module,
        "load_project_agent_settings",
        AsyncMock(return_value=settings),
    )
    monkeypatch.setattr(worker_module, "load_system_secret", AsyncMock(return_value="oauth"))
    monkeypatch.setattr(
        worker_module,
        "load_conversation_context",
        AsyncMock(return_value=context),
    )
    monkeypatch.setattr(worker_module, "append_conversation_message", AsyncMock())
    monkeypatch.setattr(worker_module, "enqueue_job", AsyncMock())
    monkeypatch.setattr(worker_module, "append_audit", AsyncMock())

    await worker._answer_interaction_impl(interaction.id)

    assert len(calls) == 2
    assert calls[0]["resume_session"] is True
    assert calls[0]["session_id"] == old_session_id
    assert calls[0]["conversation_context"] is None
    assert calls[1]["resume_session"] is False
    assert calls[1]["session_id"] != old_session_id
    assert calls[1]["conversation_context"]["summary"] == context.summary
    assert thread.claude_session_id == calls[1]["session_id"]
    assert interaction.provider_metadata["session_rotated_after_resume_failure"] is True


@pytest.mark.parametrize("membership_revoked", [False, True])
@pytest.mark.asyncio
async def test_changed_live_policy_rejects_answer_before_persist(
    monkeypatch,
    membership_revoked: bool,
) -> None:
    project_id = uuid4()
    repository = Repository(
        id=uuid4(),
        project_id=project_id,
        name="backend",
        ssh_url="git@github.com:example/backend.git",
        allowed_paths=["src"],
    )
    interaction = Interaction(
        id=uuid4(),
        project_id=project_id,
        repository_id=repository.id,
        correlation_id="mcp:policy-change",
        source="mcp",
        source_ref={},
        question="Как работает API?",
        commit_sha="a" * 40,
        status="queued",
        conversation_thread_id=None,
    )

    def agent_settings(effort: str) -> SimpleNamespace:
        return SimpleNamespace(
            enabled=True,
            memory_enabled=False,
            memory_recent_messages=24,
            memory_max_context_chars=24_000,
            denied_globs=[],
            telegram_streaming_enabled=False,
            privacy_level="balanced",
            claude_model="sonnet",
            claude_effort=effort,
            base_prompt="",
            answer_style="normal",
        )

    initial_settings = agent_settings("medium")
    changed_settings = agent_settings("high")

    class FakeSession:
        async def get(self, model, _key):
            if model is Interaction:
                return interaction
            if model is Repository:
                return repository
            return None

        async def flush(self) -> None:
            return None

        def expunge(self, _value) -> None:
            return None

    @asynccontextmanager
    async def session():
        yield FakeSession()

    result = SimpleNamespace(
        answer=KnowledgeAnswer(
            answer_markdown="Ответ из уже устаревшего контекста.",
            context_attestation={
                "contract_version": "dca-context-v1",
                "nonce": "1" * 32,
                "policy_sha256": "2" * 64,
                "context_sha256": "3" * 64,
            },
        ),
        accepted_citations=[],
        rejected_citations=[],
        cli_version="test",
        session_id=None,
        compaction_count=0,
        context_metadata={"contract_version": "dca-context-v1"},
    )
    worker = Worker.__new__(Worker)
    worker.settings = SimpleNamespace(session_secret=SimpleNamespace(get_secret_value=lambda: "x"))
    worker.database = SimpleNamespace(session=session)
    worker.snapshots = SimpleNamespace(materialize=AsyncMock(return_value=Path("/snapshot")))
    worker.claude = SimpleNamespace(answer=AsyncMock(return_value=result))
    worker.telegram = SimpleNamespace()
    monkeypatch.setattr(
        worker_module,
        "load_project_agent_settings",
        AsyncMock(
            side_effect=[
                initial_settings,
                initial_settings if membership_revoked else changed_settings,
            ]
        ),
    )
    monkeypatch.setattr(worker_module, "load_system_secret", AsyncMock(return_value="oauth"))
    if membership_revoked:
        monkeypatch.setattr(
            worker_module,
            "load_live_requester_profile",
            AsyncMock(
                side_effect=[
                    None,
                    ServiceError(
                        "project_scope_violation",
                        "Requester no longer has access to this project",
                    ),
                ]
            ),
        )

    with pytest.raises(ClaudeError) as error:
        await worker._answer_interaction_impl(interaction.id)

    assert error.value.code == "context_policy_changed"
    assert error.value.retryable is True
    assert interaction.status == "generating"
    assert interaction.answer_markdown is None


@pytest.mark.parametrize(
    ("question", "expected_attach"),
    [
        ("Как подключить аватарки?", False),
        ("Создай документацию по аватаркам", True),
    ],
)
@pytest.mark.asyncio
async def test_publish_attaches_markdown_only_when_requested(
    monkeypatch,
    question: str,
    expected_attach: bool,
) -> None:
    artifact = {"name": "avatars.md", "content": "# Avatars"}
    project_id = uuid4()
    repository = Repository(
        id=uuid4(),
        project_id=project_id,
        name="backend",
        ssh_url="git@github.com:example/backend.git",
        allowed_paths=[],
    )
    settings = SimpleNamespace(
        enabled=True,
        telegram_attach_markdown=True,
        denied_globs=[],
        claude_model="sonnet",
        claude_effort="medium",
        privacy_level="strict",
        memory_enabled=True,
        memory_recent_messages=24,
        memory_max_context_chars=24_000,
        base_prompt="",
        answer_style="normal",
    )
    live_profile = {
        "user_id": str(uuid4()),
        "telegram_user_id": 9001,
        "display_name": "Frontend developer",
        "role": "frontend",
        "department": "Frontend",
        "stack": "JavaScript",
        "language": "ru",
        "knowledge_scope": "integration",
        "can_create_requests": True,
    }
    policy_hash = compile_agent_policy(
        project_settings=settings,
        requester_profile=live_profile,
        delivery_scope="external",
        repository_allowed_paths=[],
        repository_denied_globs=[],
    ).policy_sha256
    interaction = Interaction(
        id=uuid4(),
        project_id=project_id,
        repository_id=repository.id,
        correlation_id="tg:publish",
        source="telegram",
        source_ref={"telegram_user_id": 9001},
        question=question,
        status="answer_ready",
        answer_markdown="Готовый ответ",
        artifacts=[artifact],
        provider_metadata={"native_context": {"policy_sha256": policy_hash}},
    )
    interaction.provider_metadata["document_requested"] = expected_attach

    class FakeSession:
        async def get(self, model, key):
            if model is Interaction:
                assert key == interaction.id
                return interaction
            if model is Repository:
                assert key == repository.id
                return repository
            return None

        def expunge(self, value) -> None:
            assert value is interaction

    @asynccontextmanager
    async def session():
        yield FakeSession()

    publish = AsyncMock()
    publish_error = AsyncMock()
    worker = Worker.__new__(Worker)
    worker.database = SimpleNamespace(session=session)
    worker.telegram = SimpleNamespace(
        publish_knowledge_answer=publish,
        publish_knowledge_error=publish_error,
    )
    monkeypatch.setattr(
        worker_module,
        "load_project_agent_settings",
        AsyncMock(return_value=settings),
    )
    monkeypatch.setattr(
        worker_module,
        "load_live_requester_profile",
        AsyncMock(return_value=live_profile),
    )
    monkeypatch.setattr(worker_module, "append_audit", AsyncMock())

    await worker._publish_interaction(interaction.id)

    publish.assert_awaited_once_with(
        interaction,
        "Готовый ответ",
        artifacts=[artifact] if expected_attach else [],
        attach_markdown=expected_attach,
    )


@pytest.mark.asyncio
async def test_publish_fails_closed_when_live_policy_changed(monkeypatch) -> None:
    project_id = uuid4()
    repository = Repository(
        id=uuid4(),
        project_id=project_id,
        name="backend",
        ssh_url="git@github.com:example/backend.git",
        allowed_paths=["src"],
    )

    def settings(effort: str) -> SimpleNamespace:
        return SimpleNamespace(
            enabled=True,
            telegram_attach_markdown=True,
            denied_globs=[],
            claude_model="sonnet",
            claude_effort=effort,
            privacy_level="strict",
            memory_enabled=True,
            memory_recent_messages=24,
            memory_max_context_chars=24_000,
            base_prompt="",
            answer_style="normal",
        )

    generated_policy_hash = compile_agent_policy(
        project_settings=settings("medium"),
        requester_profile=None,
        delivery_scope="external",
        repository_allowed_paths=repository.allowed_paths,
        repository_denied_globs=[],
    ).policy_sha256
    interaction = Interaction(
        id=uuid4(),
        project_id=project_id,
        repository_id=repository.id,
        correlation_id="tg:stale-publish",
        source="telegram",
        source_ref={},
        question="Как работает API?",
        status="answer_ready",
        answer_markdown="Устаревший ответ",
        artifacts=[],
        provider_metadata={"native_context": {"policy_sha256": generated_policy_hash}},
    )

    class FakeSession:
        async def get(self, model, key):
            if model is Interaction:
                assert key == interaction.id
                return interaction
            if model is Repository:
                assert key == repository.id
                return repository
            return None

        def expunge(self, value: object) -> None:
            assert value is interaction

    @asynccontextmanager
    async def session():
        yield FakeSession()

    publish = AsyncMock()
    publish_error = AsyncMock()
    audit = AsyncMock()
    worker = Worker.__new__(Worker)
    worker.database = SimpleNamespace(session=session)
    worker.telegram = SimpleNamespace(
        publish_knowledge_answer=publish,
        publish_knowledge_error=publish_error,
    )
    monkeypatch.setattr(
        worker_module,
        "load_project_agent_settings",
        AsyncMock(return_value=settings("high")),
    )
    monkeypatch.setattr(worker_module, "append_audit", audit)

    result = await worker._publish_interaction(interaction.id)

    assert result["policy_blocked"] is True
    assert result["accepted_by_telegram"] is True
    assert interaction.status == "failed"
    assert interaction.error_code == "context_policy_changed"
    publish.assert_not_awaited()
    publish_error.assert_awaited_once_with(interaction)
    assert audit.await_args.kwargs["event_type"] == "knowledge.answer_publish_policy_blocked"


@pytest.mark.parametrize(
    "error, expected_code",
    [
        (
            TelegramNetworkError(
                method=SendMessage(chat_id=1, text="test"),
                message="connection lost after send",
            ),
            "telegram_network_timeout",
        ),
        (
            TelegramRetryAfter(
                method=SendMessage(chat_id=1, text="test"),
                message="rate limited after an earlier action",
                retry_after=3,
            ),
            "telegram_rate_limited",
        ),
    ],
    ids=["network", "rate-limit"],
)
@pytest.mark.asyncio
async def test_process_update_telegram_error_is_delivery_uncertain(
    monkeypatch,
    error: Exception,
    expected_code: str,
) -> None:
    worker = Worker.__new__(Worker)
    job = Job(
        id=uuid4(),
        kind="telegram.process_update",
        payload={"update_id": 42},
        attempts=1,
        max_attempts=5,
    )

    async def fail_dispatch(_: Job) -> dict[str, Any]:
        raise error

    uncertain = AsyncMock()
    retry = AsyncMock()
    monkeypatch.setattr(worker, "_dispatch", fail_dispatch)
    monkeypatch.setattr(worker, "_delivery_uncertain", uncertain)
    monkeypatch.setattr(worker, "_retry", retry)

    await worker.process(job)

    uncertain.assert_awaited_once()
    assert uncertain.await_args.args[:2] == (job, expected_code)
    retry.assert_not_awaited()


@pytest.mark.parametrize("kind", sorted(TELEGRAM_EXTERNAL_ACTIONS))
@pytest.mark.asyncio
async def test_generic_error_never_replays_external_action(monkeypatch, kind: str) -> None:
    worker = Worker.__new__(Worker)
    job = Job(
        id=uuid4(),
        kind=kind,
        payload={},
        attempts=1,
        max_attempts=5,
    )

    async def fail_dispatch(_: Job) -> dict[str, Any]:
        raise SQLAlchemyError("commit failed after external action")

    uncertain = AsyncMock()
    retry = AsyncMock()
    monkeypatch.setattr(worker, "_dispatch", fail_dispatch)
    monkeypatch.setattr(worker, "_delivery_uncertain", uncertain)
    monkeypatch.setattr(worker, "_retry", retry)

    await worker.process(job)

    uncertain.assert_awaited_once_with(job, "external_action_failed", "SQLAlchemyError")
    retry.assert_not_awaited()
