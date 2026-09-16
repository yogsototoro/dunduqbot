"""
The process that fills the pool · it wakes on a signal, spends what the day
allows and sleeps again
"""

import asyncio
import os
from collections.abc import Callable
from typing import Any

import aiohttp
from redis.asyncio import Redis

from bot.shared.store import (
    BLOCK_SECONDS,
    COOLDOWNS,
    LINKS,
    LINKS_PORTION,
    QUOTAS,
    TRIES,
    WAKE,
    budget_key,
    connect,
    cooldown_key,
    raise_wake,
    today,
    tries_key,
)

BUDGET_TTL = 172800
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)

# one answer from TheCatAPI is 100 links
CAT_LINKS_URL = f"https://api.thecatapi.com/v1/images/search?limit={LINKS_PORTION}"


def cat_links(data: Any) -> list[str] | None:
    """
    An answer from TheCatAPI is a list of images · an item carrying no url is passed over
    """
    if not isinstance(data, list):
        return None
    return [item["url"] for item in data if isinstance(item, dict) and "url" in item]


async def fetch_service(
    session: aiohttp.ClientSession, url: str, api_key: str, read: Callable
) -> Any | None:
    """
    Every service is asked the same way · the reader says what a 200 was worth, and an
    answer no reader can use reads as no answer at all
    """
    try:
        async with session.get(
            url, headers={"x-api-key": api_key}, timeout=REQUEST_TIMEOUT
        ) as resp:
            return read(await resp.json()) if resp.status == 200 else None
    except Exception:
        return None


async def room_left(redis: Redis, key: str, ceiling: int) -> bool:
    """
    The key carries the date · it says whether the day has anything left to give
    """
    current = await redis.get(key)
    return current is None or int(current) < ceiling


async def spend(redis: Redis, key: str):
    """
    One step off a daily counter · the key expires two days later on its own
    """
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, BUDGET_TTL)
        await pipe.execute()


async def fetch_within_budget(redis: Redis, svc: str, day_str: str, fetch):
    """
    One request per signal · a try is spent on every request and the budget only on an
    answer, and a request that brought nothing starts a cooldown. So a source that is
    down keeps the day's answers and is asked again later, for as long as the day lasts
    """
    budget = budget_key(svc, day_str)
    tries = tries_key(svc, day_str)

    if await redis.exists(cooldown_key(svc)):
        return None
    if not await room_left(redis, budget, QUOTAS[svc]):
        return None
    if not await room_left(redis, tries, TRIES[svc]):
        return None

    await spend(redis, tries)
    result = await fetch()
    if result is None:
        await redis.set(cooldown_key(svc), "1", ex=COOLDOWNS[svc])
        return None

    await spend(redis, budget)
    return result


async def fill_links(
    redis: Redis, session: aiohttp.ClientSession, signal: str, catapi_key: str
):
    """
    daily_run replaces the pool, anything else tops it up
    """
    links = await fetch_within_budget(
        redis,
        "thecatapi",
        today(),
        lambda: fetch_service(session, CAT_LINKS_URL, catapi_key, cat_links),
    )
    if not links:
        return

    if signal == "daily_run":
        await redis.delete(LINKS)
    await redis.rpush(LINKS, *links)


async def refiller_loop():
    """
    The loop waits on the wake list · what a start finds short is topped up once, so a
    restart is no reason to spend the quota again
    """
    redis_url = os.environ["REDIS_URL"]
    catapi_key = os.environ["CATAPI_KEY"]

    redis = connect(redis_url)

    async with aiohttp.ClientSession() as session:
        await raise_wake(redis)

        while True:
            result = await redis.brpop(WAKE, timeout=BLOCK_SECONDS)
            if not result:
                continue
            _, signal_raw = result
            await fill_links(redis, session, str(signal_raw), catapi_key)


def main():
    """
    The refiller process: one connection, one session, and a wake list to wait on
    """
    asyncio.run(refiller_loop())


if __name__ == "__main__":
    main()
