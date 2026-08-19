"""
Transactional outbox.

The gateway records a payment in PostgreSQL and publishes its task to Redis.
Those are two systems with no shared transaction, so between the commit and the
publish there is a window: a process that dies there leaves a card authorised
and nothing queued to deliver against it.

The outbox closes the window by writing the intent into the same commit as the
payment. Publishing then becomes a separate, retryable step reading from a
durable record instead of from a variable in a dying process.

Two properties are worth stating plainly, because they shape everything here:

  * **At-least-once, never at-most-once.** A row is marked published only after
    the broker accepts it, so a crash in between republishes. Both gateway tasks
    already refuse to act twice on the same transaction, which is what makes
    duplicate delivery cheaper than loss.
  * **The fast path is an optimisation, not the mechanism.** The API tries to
    publish immediately so latency is unchanged, but that attempt is allowed to
    fail silently: the relay is what guarantees delivery.
"""

import json
import logging
from datetime import timedelta
from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.database import async_session
from gateway.models import OutboxMessage, utcnow

LOGGER = logging.getLogger(__name__)

#: How many publish attempts before a message is set aside for a human. The
#: payment it describes is real, so it is flagged rather than discarded.
MAX_ATTEMPTS = 10


def enqueue(
    session: AsyncSession,
    task_name: str,
    payload: Dict[str, Any],
    countdown: int = 0,
) -> OutboxMessage:
    """Record a task to publish, inside the caller's transaction.

    Deliberately not async and deliberately does not commit: it adds to the
    session the caller already holds, so the message and the payment it refers
    to share one commit. Committing here would recreate the very window this
    exists to close.
    """
    message = OutboxMessage(
        task_name=task_name,
        payload=json.dumps(payload, default=str),
        countdown=countdown,
    )
    session.add(message)
    return message


async def publish_one(message: OutboxMessage) -> bool:
    """Hand a single message to the broker. Returns whether it was accepted."""
    from gateway.worker import celery_app

    kwargs = json.loads(message.payload)
    celery_app.send_task(
        message.task_name,
        kwargs=kwargs,
        countdown=message.countdown or None,
    )
    return True


async def try_publish_now(message_id: int) -> bool:
    """Publish immediately, for latency. Failure here is not an error.

    The relay will pick the message up on its next pass, which is the actual
    guarantee. This path exists only so the common case does not wait for it.
    """
    try:
        async with async_session() as session:
            message = await session.get(OutboxMessage, message_id)
            if message is None or message.published_at is not None:
                return False

            await publish_one(message)
            message.published_at = utcnow()
            message.attempts += 1
            await session.commit()
            return True
    except Exception as exc:
        LOGGER.warning(
            f"Immediate publish of outbox message {message_id} failed ({exc}); "
            "the relay will retry it."
        )
        return False


async def publish_pending(limit: int = 100) -> Dict[str, int]:
    """Publish everything still waiting. This is the durability guarantee.

    Runs on a schedule and after any broker outage. Rows are taken oldest first
    and locked for update, so several relays can run without publishing the same
    message twice.
    """
    counts = {"published": 0, "failed": 0, "abandoned": 0}

    async with async_session() as session:
        statement = (
            select(OutboxMessage)
            .where(
                OutboxMessage.published_at.is_(None),
                OutboxMessage.abandoned.is_(False),
            )
            .order_by(OutboxMessage.created_at)
            .limit(limit)
        )
        # SKIP LOCKED so a second relay works on different rows rather than
        # blocking behind the first. Ignored by SQLite, which has one writer.
        if session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update(skip_locked=True)

        pending = (await session.execute(statement)).scalars().all()

        for message in pending:
            message.attempts += 1
            try:
                await publish_one(message)
                message.published_at = utcnow()
                counts["published"] += 1
            except Exception as exc:
                message.last_error = str(exc)[:512]
                counts["failed"] += 1

                if message.attempts >= MAX_ATTEMPTS:
                    message.abandoned = True
                    counts["abandoned"] += 1
                    LOGGER.critical(
                        f"MANUAL ACTION REQUIRED — outbox message {message.id} "
                        f"({message.task_name}) could not be published after "
                        f"{message.attempts} attempts: {exc}. The payment it "
                        "describes has not been acted on."
                    )
                else:
                    LOGGER.warning(
                        f"Outbox message {message.id} failed to publish "
                        f"(attempt {message.attempts}): {exc}"
                    )

        await session.commit()

    if counts["published"]:
        LOGGER.info(f"Outbox relay published {counts['published']} message(s).")
    return counts


async def purge_published(older_than_days: int = 7) -> int:
    """Remove messages that were delivered long enough ago to be uninteresting.

    Kept for a while rather than deleted on publish: when a payment is queried
    days later, "was this ever dispatched, and when?" is a question worth being
    able to answer.
    """
    cutoff = utcnow() - timedelta(days=older_than_days)

    async with async_session() as session:
        old = (
            await session.execute(
                select(OutboxMessage).where(
                    OutboxMessage.published_at.isnot(None),
                    OutboxMessage.published_at < cutoff,
                )
            )
        ).scalars().all()

        for message in old:
            await session.delete(message)
        await session.commit()

    if old:
        LOGGER.info(f"Purged {len(old)} published outbox message(s).")
    return len(old)


async def pending_count() -> int:
    """How many messages are waiting. A readiness and alerting signal."""
    async with async_session() as session:
        rows = (
            await session.execute(
                select(OutboxMessage).where(
                    OutboxMessage.published_at.is_(None),
                    OutboxMessage.abandoned.is_(False),
                )
            )
        ).scalars().all()
        return len(rows)


async def abandoned_count() -> int:
    """Messages that gave up. Each one is a payment nobody acted on."""
    async with async_session() as session:
        rows = (
            await session.execute(
                select(OutboxMessage).where(OutboxMessage.abandoned.is_(True))
            )
        ).scalars().all()
        return len(rows)
