import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
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
from sqlalchemy.exc import SQLAlchemyError

import dca.worker as worker_module
from dca.claude import ClaudeError
from dca.config import Settings
from dca.db import AgentMessage, Interaction, Job, Repository, TelegramChat
from dca.memory import ConversationContext, ConversationContextMessage
from dca.service import ServiceError
from dca.worker import (
    TELEGRAM_EXTERNAL_ACTIONS,
    Worker,
    conversation_prompt_context,
    trusted_requester_profile,
)


def test_telegram_mode_is_strict_and_webhook_compatible() -> None:
    assert Settings(telegram_mode="polling").telegram_mode == "polling"
    assert Settings(telegram_mode="webhook").telegram_mode == "webhook"
    with pytest.raises(ValidationError):
        Settings(telegram_mode="updates")  # type: ignore[arg-type]


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
    worker.settings = SimpleNamespace(telegram_mode="polling")
    worker.recover_stale_jobs = AsyncMock()
    worker._run_job_loop = AsyncMock()
    worker._poll_telegram_forever = AsyncMock()
    worker.telegram = SimpleNamespace(close=AsyncMock())
    worker.database = SimpleNamespace(close=AsyncMock())
    worker.worker_id = "test-worker"

    await worker.run_forever()

    worker._run_job_loop.assert_awaited_once_with()
    worker._poll_telegram_forever.assert_awaited_once_with()
    worker.telegram.close.assert_awaited_once_with()
    worker.database.close.assert_awaited_once_with()


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
            "requester_profile": {
                "display_name": "Бека",
                "department": "Mobile",
                "stack": "Android / Kotlin",
                "unknown": "must not reach Claude",
                "role": {"invalid": "type"},
            }
        },
    )

    assert trusted_requester_profile(interaction) == {
        "display_name": "Бека",
        "department": "Mobile",
        "stack": "Android / Kotlin",
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
    publish_error.assert_awaited_once_with(job, "Timed out")


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
