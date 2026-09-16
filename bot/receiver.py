"""
The process Telegram talks to · one panel to a chat, and every tap answered
from Redis alone
"""

import contextlib
import os
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from redis.asyncio import Redis

from bot.shared.logic import (
    apply_half,
    keeps_a_time,
    offset_entry,
    settled_schedule,
    time_entry_append,
)
from bot.shared.screens import (
    PANEL_PHOTO,
    CatCb,
    ClearCb,
    CloseCb,
    NavCb,
    OffsetCb,
    SaveCb,
    TimeCb,
    ToggleCb,
    build_main_screen,
    build_offset_screen,
    build_settings_screen,
    build_time_screen,
)
from bot.shared.store import (
    CHAT_DEFAULTS,
    PANELS,
    QUEUE,
    chat_key,
    chat_setting,
    connect,
    draft_key,
    encode_job,
    forget_chat,
    now_utc,
    save_chat,
    unschedule_chat,
)

WEBHOOK_PATH = "/webhook"
PORT = 9101
PANEL_LIFETIME = 600
DRAFT_LIFETIME = 600


def webhook_endpoint(base_url: str) -> str:
    """
    The path is appended once · a URL that already carries it is left alone
    """
    base = base_url.rstrip("/")
    return base if base.endswith(WEBHOOK_PATH) else f"{base}{WEBHOOK_PATH}"


async def safe_edit(panel: Message, text: str, kb: InlineKeyboardMarkup):
    """
    A tap edits the caption and the keys, never the photo · one that changes nothing is
    refused by Telegram, and a refused tap leaves the panel as it was
    """
    try:
        await panel.edit_caption(caption=text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


class PanelMiddleware(BaseMiddleware):
    """
    The panel a tap came from · it is found once here instead of in every handler
    """

    def __init__(self, redis: Redis):
        self.redis = redis

    async def __call__(
        self,
        handler: Callable[[CallbackQuery, dict[str, Any]], Awaitable[Any]],
        event: CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        """
        Every tap pushes the closing time out · a tap with no panel behind it is dropped
        """
        if not isinstance(event.message, Message):
            return None

        chat_id = event.message.chat.id
        data["chat_id"] = chat_id
        data["panel"] = event.message

        await self.redis.zadd(PANELS, {str(chat_id): now_utc().timestamp() + PANEL_LIFETIME})
        return await handler(event, data)


async def touch_draft(redis: Redis, chat_id: int, field: str, value: str | int):
    """
    A draft lives ten minutes from the last key · every write pushes that out
    """
    d_key = draft_key(chat_id)
    await redis.hset(d_key, field, value)
    await redis.expire(d_key, DRAFT_LIFETIME)


async def chat_is_24h(redis: Redis, chat_id: int) -> bool:
    """
    The keypad follows the chat · one that never chose a format reads as 24 hours
    """
    return bool(chat_setting(await redis.hgetall(chat_key(chat_id)), "format"))


async def command_start(message: Message, bot: Bot, redis: Redis):
    """
    One panel to a chat · the old one is deleted before the new one is sent
    """
    chat_id = message.chat.id
    c_key = chat_key(chat_id)

    old_panel_id = await redis.hget(c_key, "panel_id")
    if old_panel_id:
        # a panel the chat deleted by hand is gone already, and that is no error
        with contextlib.suppress(Exception):
            await bot.delete_message(chat_id, int(old_panel_id))

    text, kb = build_main_screen()
    msg = await message.answer_photo(FSInputFile(PANEL_PHOTO), caption=text, reply_markup=kb)

    async with redis.pipeline(transaction=True) as pipe:
        pipe.hset(c_key, "panel_id", msg.message_id)
        pipe.zadd(PANELS, {str(chat_id): now_utc().timestamp() + PANEL_LIFETIME})
        await pipe.execute()


async def chat_migration(message: Message, redis: Redis):
    """
    A group that becomes a supergroup gets a new id · the settings move with it
    """
    old_id = message.chat.id
    new_id = message.migrate_to_chat_id
    if new_id is None:
        return

    chat_data = await redis.hgetall(chat_key(old_id))
    settled = settled_schedule(chat_data, now_utc())

    if settled:
        offset_minutes, time_str, next_ts = settled
        await save_chat(
            redis,
            new_id,
            offset_minutes,
            time_str,
            chat_setting(chat_data, "format"),
            next_ts,
        )
    elif chat_data:
        carried = {k: v for k, v in chat_data.items() if k in ("offset", "format")}
        if carried:
            await redis.hset(chat_key(new_id), mapping=carried)

    await forget_chat(redis, old_id)


async def handle_cat(query: CallbackQuery, redis: Redis, chat_id: int):
    """
    🖼️ queues a cat outside the schedule · the sender decides what actually arrives
    """
    await redis.rpush(QUEUE, encode_job(chat_id, scheduled=False))
    await query.answer()


async def handle_nav(
    query: CallbackQuery, callback_data: NavCb, redis: Redis, chat_id: int, panel: Message
):
    """
    Moving to a screen · the two entry screens open a draft that expires on its own
    """
    chat_data = await redis.hgetall(chat_key(chat_id))

    if callback_data.screen == "settings":
        text, kb = build_settings_screen(chat_data)
    elif callback_data.screen == "offset":
        saved_off = chat_data.get("offset")
        draft_off = None if saved_off in (None, "") else int(saved_off)
        await touch_draft(redis, chat_id, "offset", "" if draft_off is None else draft_off)
        text, kb = build_offset_screen(draft_off)
    elif callback_data.screen == "time":
        draft_t = str(chat_data.get("time", ""))
        await touch_draft(redis, chat_id, "time", draft_t)
        text, kb = build_time_screen(draft_t, bool(chat_setting(chat_data, "format")))
    else:
        text, kb = build_main_screen()

    await safe_edit(panel, text, kb)
    await query.answer()


async def handle_toggle(
    query: CallbackQuery, callback_data: ToggleCb, redis: Redis, chat_id: int, panel: Message
):
    """
    A toggle needs no ✅ · a key from a setting that no longer exists is answered and ignored
    """
    field = callback_data.field
    if field not in CHAT_DEFAULTS:
        await query.answer()
        return

    c_key = chat_key(chat_id)
    chat_data = await redis.hgetall(c_key)

    current = chat_setting(chat_data, field)
    new_val = 0 if current else 1

    await redis.hset(c_key, field, new_val)
    chat_data[field] = str(new_val)

    text, kb = build_settings_screen(chat_data)
    await safe_edit(panel, text, kb)
    await query.answer()


async def handle_offset(
    query: CallbackQuery, callback_data: OffsetCb, redis: Redis, chat_id: int, panel: Message
):
    """
    ± moves the draft offset, nothing is saved yet
    """
    current_draft = int(await redis.hget(draft_key(chat_id), "offset") or 0)
    new_offset = offset_entry(current_draft, callback_data.step)

    await touch_draft(redis, chat_id, "offset", new_offset)

    text, kb = build_offset_screen(new_offset)
    await safe_edit(panel, text, kb)
    await query.answer()


async def handle_time(
    query: CallbackQuery, callback_data: TimeCb, redis: Redis, chat_id: int, panel: Message
):
    """
    A digit, a backspace or a half of the day · a key no reading survives is undone
    """
    current_draft = str(await redis.hget(draft_key(chat_id), "time") or "")

    char = callback_data.char
    if char == "bksp":
        new_draft = current_draft[:-1]
    elif char in ("am", "pm"):
        new_draft = apply_half(current_draft, char)
    else:
        new_draft = time_entry_append(current_draft, char)

    if not keeps_a_time(new_draft):
        new_draft = current_draft

    await touch_draft(redis, chat_id, "time", new_draft)

    text, kb = build_time_screen(new_draft, await chat_is_24h(redis, chat_id))
    await safe_edit(panel, text, kb)
    await query.answer()


async def handle_save(
    query: CallbackQuery, callback_data: SaveCb, redis: Redis, chat_id: int, panel: Message
):
    """
    ✅ writes the draft down · a chat is scheduled only once it has an offset and a time.
    An empty time is the chat clearing it, an empty offset is a screen nobody touched
    """
    c_key, d_key = chat_key(chat_id), draft_key(chat_id)

    draft_val = await redis.hget(d_key, callback_data.target)
    untouched_offset = callback_data.target == "offset" and draft_val == ""
    if draft_val is not None and not untouched_offset:
        await redis.hset(c_key, callback_data.target, draft_val)

    chat_data = await redis.hgetall(c_key)
    settled = settled_schedule(chat_data, now_utc())

    if settled:
        offset_minutes, time_str, next_ts = settled
        format_24h = chat_setting(chat_data, "format")

        await save_chat(redis, chat_id, offset_minutes, time_str, format_24h, next_ts)
        chat_data["time"] = time_str
    else:
        await unschedule_chat(redis, chat_id)

    text, kb = build_settings_screen(chat_data)
    await safe_edit(panel, text, kb)
    await query.answer()


async def handle_clear(query: CallbackQuery, redis: Redis, chat_id: int, panel: Message):
    """
    -- : -- empties the draft and stays on the keypad · nothing is saved until ✅
    """
    await touch_draft(redis, chat_id, "time", "")

    text, kb = build_time_screen("", await chat_is_24h(redis, chat_id))
    await safe_edit(panel, text, kb)
    await query.answer()


async def handle_close(query: CallbackQuery, redis: Redis, chat_id: int, panel: Message):
    """
    ❎ deletes the panel · the settings stay, only the message goes
    """
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hdel(chat_key(chat_id), "panel_id")
        pipe.zrem(PANELS, str(chat_id))
        await pipe.execute()

    await panel.delete()
    await query.answer()


async def on_startup(bot: Bot, webhook_url: str, webhook_secret: str):
    """
    The webhook is registered on every start · only the two updates the panel needs
    """
    await bot.set_webhook(
        url=webhook_endpoint(webhook_url),
        secret_token=webhook_secret,
        allowed_updates=["message", "callback_query"],
    )


async def health(request: web.Request) -> web.Response:
    """
    The deploy asks whether the receiver came up · it answers that and nothing else
    """
    return web.Response(text="ok")


def build_app(bot: Bot, dp: Dispatcher, webhook_secret: str) -> web.Application:
    """
    One port carries the webhook · the secret is checked by the handler
    """
    app = web.Application()
    app.router.add_get("/health", health)
    handler = SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=webhook_secret)
    handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    return app


def build_dispatcher(redis: Redis) -> Dispatcher:
    """
    Every callback has one handler · they are matched by prefix, in the order registered
    """
    dp = Dispatcher(redis=redis)

    dp.callback_query.middleware(PanelMiddleware(redis))

    dp.message.register(command_start, CommandStart())
    dp.message.register(chat_migration, lambda message: message.migrate_to_chat_id is not None)

    dp.callback_query.register(handle_cat, CatCb.filter())
    dp.callback_query.register(handle_nav, NavCb.filter())
    dp.callback_query.register(handle_toggle, ToggleCb.filter())
    dp.callback_query.register(handle_offset, OffsetCb.filter())
    dp.callback_query.register(handle_time, TimeCb.filter())
    dp.callback_query.register(handle_save, SaveCb.filter())
    dp.callback_query.register(handle_clear, ClearCb.filter())
    dp.callback_query.register(handle_close, CloseCb.filter())

    return dp


def main():
    """
    The receiver process: settings from the environment, then the web app
    """
    bot_token = os.environ["BOT_TOKEN"]
    webhook_url = os.environ["WEBHOOK_URL"]
    webhook_secret = os.environ["WEBHOOK_SECRET"]
    redis_url = os.environ["REDIS_URL"]

    redis = connect(redis_url)
    bot = Bot(token=bot_token)
    dp = build_dispatcher(redis)

    async def on_startup_wrapper(bot: Bot):
        await on_startup(bot, webhook_url, webhook_secret)

    dp.startup.register(on_startup_wrapper)

    web.run_app(
        build_app(bot, dp, webhook_secret),
        host="0.0.0.0",
        port=PORT,
        access_log=None,
        print=lambda _: None,
    )


if __name__ == "__main__":
    main()
