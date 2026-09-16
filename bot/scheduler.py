"""
The process that keeps time · it moves the moment, queues the send and closes
the panels nobody touched
"""

import asyncio
import contextlib
import datetime
import os

from aiogram import Bot
from redis.asyncio import Redis

from bot.shared.logic import settled_schedule
from bot.shared.store import (
    CLAIM_DUE_SCRIPT,
    PANELS,
    QUEUE,
    SCHEDULE,
    WAKE,
    chat_key,
    connect,
    encode_job,
    now_utc,
)


async def sweep_panels(redis: Redis, bot: Bot, now: datetime.datetime):
    """
    The panel closes ten minutes after the last tap · the bot keeps no timer of its own
    """
    expired_panels = await redis.zrangebyscore(PANELS, "-inf", now.timestamp())
    for chat_id_raw in expired_panels:
        chat_id_str = str(chat_id_raw)
        c_key = chat_key(int(chat_id_str))
        panel_id = await redis.hget(c_key, "panel_id")

        async with redis.pipeline(transaction=True) as pipe:
            pipe.zrem(PANELS, chat_id_str)
            pipe.hdel(c_key, "panel_id")
            await pipe.execute()

        if panel_id:
            # a panel the chat deleted by hand is gone already, and that is no error
            with contextlib.suppress(Exception):
                await bot.delete_message(int(chat_id_str), int(str(panel_id)))


async def queue_due_chats(redis: Redis, claim, now: datetime.datetime):
    """
    The scheduler moves the moment and queues the job in one script · a chat whose
    settings no longer read as a schedule leaves it instead
    """
    due_chats = await redis.zrangebyscore(SCHEDULE, "-inf", now.timestamp(), withscores=True)
    for chat_id_raw, due_ts in due_chats:
        chat_id_str = str(chat_id_raw)
        chat_id = int(chat_id_str)
        chat_data = await redis.hgetall(chat_key(chat_id))

        settled = settled_schedule(chat_data, now)
        if settled is None:
            await redis.zrem(SCHEDULE, chat_id_str)
            continue

        _, _, next_ts = settled
        await claim(
            keys=[SCHEDULE, QUEUE],
            args=[chat_id_str, due_ts, next_ts, encode_job(chat_id, scheduled=True)],
        )


async def tick(redis: Redis, bot: Bot, claim, last_day: int) -> int:
    """
    One second of work: the panels and the schedule · the day carries between ticks
    """
    now = now_utc()

    if now.day != last_day:
        await redis.rpush(WAKE, "daily_run")
        last_day = now.day

    await sweep_panels(redis, bot, now)
    await queue_due_chats(redis, claim, now)

    return last_day


async def scheduler_loop():
    """
    A tick that cannot finish ends the process · the supervisor starts it again
    """
    bot_token = os.environ["BOT_TOKEN"]
    redis_url = os.environ["REDIS_URL"]

    redis = connect(redis_url)
    bot = Bot(token=bot_token)
    claim = redis.register_script(CLAIM_DUE_SCRIPT)

    last_day = now_utc().day

    while True:
        last_day = await tick(redis, bot, claim, last_day)
        await asyncio.sleep(1)


def main():
    """
    The scheduler process: one connection, one bot, and a tick a second
    """
    asyncio.run(scheduler_loop())


if __name__ == "__main__":
    main()
