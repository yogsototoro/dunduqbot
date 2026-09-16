"""
The process that spends the pool · one job at a time, and what it fails to
finish goes back to the queue
"""

import asyncio
import os

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from redis.asyncio import Redis

from bot.shared.logic import Outcome
from bot.shared.screens import outcome_message
from bot.shared.store import (
    BLOCK_SECONDS,
    LINKS,
    QUEUE,
    QUEUE_WORKING,
    chat_key,
    connect,
    decode_job,
    forget_chat,
    raise_wake,
    rate_key,
)

ATTEMPTS = 3


async def take_link(redis: Redis) -> str | None:
    """
    One link leaves the pool and never comes back · an empty pool reads as None
    """
    photo_raw = await redis.lpop(LINKS)
    return str(photo_raw) if photo_raw else None


async def draw_outcome(
    redis: Redis, chat_id: int, scheduled: bool
) -> tuple[Outcome, str | None]:
    """
    A tap claims rate:{chat}, a scheduled send passes by · an empty pool decides
    """
    if not scheduled:
        acquired = await redis.set(rate_key(chat_id), "1", ex=1, nx=True)
        if not acquired:
            return Outcome.RATE_LIMITED, None

    photo_url = await take_link(redis)
    if not photo_url:
        return Outcome.NO_LINKS, None

    return Outcome.PHOTO, photo_url


async def deliver(bot: Bot, redis: Redis, chat_id: int, outcome: Outcome, photo_url: str | None):
    """
    A retry_after is slept off and tried again, three attempts · a 403 calls forget_chat.
    A photo Telegram refuses it will refuse again, so the next attempt takes the next link.
    A tap that came too soon is answered by nothing at all
    """
    if outcome == Outcome.RATE_LIMITED:
        return

    for remaining in reversed(range(ATTEMPTS)):
        try:
            if outcome == Outcome.PHOTO and photo_url:
                await bot.send_photo(chat_id, photo_url)
            else:
                await bot.send_message(chat_id, outcome_message(outcome))
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except TelegramForbiddenError:
            await forget_chat(redis, chat_id)
            return
        except TelegramBadRequest:
            if not remaining:
                return
            if outcome == Outcome.PHOTO:
                photo_url = await take_link(redis)
                if not photo_url:
                    return
            else:
                await asyncio.sleep(1)
        except TelegramAPIError:
            if remaining:
                await asyncio.sleep(1)


async def process_job(redis: Redis, bot: Bot, chat_id: int, scheduled: bool):
    """
    One job from the queue: draw the outcome, then send it
    """
    if not await redis.exists(chat_key(chat_id)):
        return

    outcome, photo_url = await draw_outcome(redis, chat_id, scheduled)

    await raise_wake(redis)
    await deliver(bot, redis, chat_id, outcome, photo_url)


async def sender_loop():
    """
    Jobs left by a killed process return to the queue before anything new is taken
    """
    bot_token = os.environ["BOT_TOKEN"]
    redis_url = os.environ["REDIS_URL"]

    redis = connect(redis_url)
    bot = Bot(token=bot_token)

    while await redis.rpoplpush(QUEUE_WORKING, QUEUE):
        pass

    while True:
        job_raw = await redis.brpoplpush(QUEUE, QUEUE_WORKING, timeout=BLOCK_SECONDS)
        if not job_raw:
            continue
        job_str = str(job_raw)
        try:
            chat_id, scheduled = decode_job(job_str)
            await process_job(redis, bot, chat_id, scheduled)
        finally:
            await redis.lrem(QUEUE_WORKING, 1, job_str)


def main():
    """
    The sender process: one connection, one bot, and one job at a time
    """
    asyncio.run(sender_loop())


if __name__ == "__main__":
    main()
