"""
The shape of everything in Redis · the keys, the scripts, and the writes that
have to happen together
"""

import datetime

from redis.asyncio import Redis

SCHEDULE = "schedule"
PANELS = "panels"
LINKS = "links"
QUEUE = "queue"
QUEUE_WORKING = "queue:working"
WAKE = "wake"

LINKS_PORTION = 100

QUOTAS = {"thecatapi": 300}
TRIES = {"thecatapi": 600}
COOLDOWNS = {"thecatapi": 60}

CHAT_DEFAULTS = {"format": 1}

BLOCK_SECONDS = 5
SOCKET_TIMEOUT = BLOCK_SECONDS * 2


def chat_key(chat_id: int) -> str:
    """
    What the chat has settled on: offset, time, format, the open panel
    """
    return f"chat:{chat_id}"


def draft_key(chat_id: int) -> str:
    """
    What the chat is typing right now · it expires on its own if nobody finishes
    """
    return f"draft:{chat_id}"


def rate_key(chat_id: int) -> str:
    """
    One tap a second · the key exists for as long as the chat has to wait
    """
    return f"rate:{chat_id}"


def budget_key(svc: str, day: str) -> str:
    """
    Answers taken from one service on one day · the date is part of the key
    """
    return f"budget:{svc}:{day}"


def tries_key(svc: str, day: str) -> str:
    """
    Requests made to one service on one day, answered or not · a source that is down
    spends these, so an outage costs the day its tries and not its answers
    """
    return f"tries:{svc}:{day}"


def cooldown_key(svc: str) -> str:
    """
    Set only when a request brought nothing · while it lives the service is left alone,
    and it expires on its own, so an outage is retried all day instead of once
    """
    return f"cooldown:{svc}"


def now_utc() -> datetime.datetime:
    """
    The instant a moment is counted from · every process reads the clock the same way
    """
    return datetime.datetime.now(datetime.UTC)


def today() -> str:
    """
    The day a budget is counted by · every process reads the date the same way
    """
    return now_utc().strftime("%Y-%m-%d")


def chat_setting(chat_data: dict, field: str) -> int:
    """
    What the chat chose · what it never chose, and what it somehow wrote down
    unreadably, both read as 24 hours
    """
    default = CHAT_DEFAULTS[field]
    try:
        return int(chat_data.get(field, default))
    except (TypeError, ValueError):
        return default


def connect(redis_url: str) -> Redis:
    """
    One store, one way of reaching it
    """
    return Redis.from_url(redis_url, decode_responses=True, socket_timeout=SOCKET_TIMEOUT)


def encode_job(chat_id: int, scheduled: bool) -> str:
    """
    Two fields on one line · the queue holds strings and nothing else
    """
    return f"{chat_id}:{1 if scheduled else 0}"


def decode_job(raw: str) -> tuple[int, bool]:
    """
    Only the first two fields are read · a job left in the queue by an older deploy
    carried a third, and one the sender cannot read would kill it on every restart
    """
    chat_id, scheduled = str(raw).split(":")[:2]
    return int(chat_id), bool(int(scheduled))


CLAIM_DUE_SCRIPT = """
local chat = ARGV[1]
local seen = tonumber(ARGV[2])
local next_moment = tonumber(ARGV[3])
local job = ARGV[4]
local score = redis.call('ZSCORE', KEYS[1], chat)
if not score or tonumber(score) > seen then return 0 end
redis.call('ZADD', KEYS[1], next_moment, chat)
redis.call('RPUSH', KEYS[2], job)
return 1
"""


async def raise_wake(redis: Redis):
    """
    Every chat spends one link · a pool that cannot serve the chats it answers to, with
    one to spare, raises the signal to fill it
    """
    if await redis.llen(LINKS) < await redis.zcard(SCHEDULE) + 1:
        await redis.rpush(WAKE, "fill")


async def save_chat(
    redis: Redis,
    chat_id: int,
    offset: int,
    time_str: str,
    format_24h: int,
    next_moment_ts: float,
):
    """
    The settings and the next moment are written together
    """
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hset(
            chat_key(chat_id), mapping={"offset": offset, "time": time_str, "format": format_24h}
        )
        pipe.zadd(SCHEDULE, {str(chat_id): next_moment_ts})
        await pipe.execute()


async def unschedule_chat(redis: Redis, chat_id: int):
    """
    A chat that no longer keeps a time leaves the schedule · its settings stay behind
    """
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hdel(chat_key(chat_id), "time")
        pipe.zrem(SCHEDULE, str(chat_id))
        await pipe.execute()


async def forget_chat(redis: Redis, chat_id: int):
    """
    A blocked bot leaves nothing behind · every key the chat ever had goes at once
    """
    key = chat_key(chat_id)
    chat_id_str = str(chat_id)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.delete(key)
        pipe.delete(draft_key(chat_id))
        pipe.delete(rate_key(chat_id))
        pipe.zrem(SCHEDULE, chat_id_str)
        pipe.zrem(PANELS, chat_id_str)
        await pipe.execute()
