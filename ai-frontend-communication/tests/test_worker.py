from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy.exc import SQLAlchemyError

from dca.claude import ClaudeError
from dca.db import Job
from dca.worker import TELEGRAM_EXTERNAL_ACTIONS, Worker


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
