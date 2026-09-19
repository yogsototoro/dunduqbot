"""
Everything the chat can see · the text, the keys, and the callbacks they send back
"""

from pathlib import Path

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.shared.logic import Outcome, draft_display, format_offset
from bot.shared.store import chat_setting


class Kaomoji:
    MAIN = "/ᐠ ｡ꞈ｡ ᐟ\\"
    SETTINGS = "ฅ⁠^｡⁠ꞈ⁠｡⁠⁠^⁠ฅ"


class Icon:
    CAT = "🖼️"
    SETTINGS = "⚙️"
    TIMEZONE = "🌍"
    TIME = "🕔"
    BACK = "↩️"
    BACKSPACE = "⌫"
    CONFIRM = "✅"
    CLOSE = "❎"


class Placeholder:
    TIME = "--:--"
    OFFSET = "--"


class Format:
    H24 = "24"
    H12 = "12"


class Step:
    HOUR = "1"
    MINUTE = "15"


class Sign:
    BUTTON_PLUS = "+"
    BUTTON_MINUS = "−"


class Half:
    FIRST = "¹"
    SECOND = "²"


OUTCOME_MESSAGE = {
    Outcome.NO_LINKS: "/ᐠ- ꞈ-ᐟ\\ 💤",
    Outcome.BROKEN: "🚧 (*/ω＼*)",
}


def outcome_message(outcome: Outcome) -> str:
    """
    What the chat is told when no cat arrives · anything unforeseen reads as broken
    """
    return OUTCOME_MESSAGE.get(outcome, OUTCOME_MESSAGE[Outcome.BROKEN])


PANEL_PHOTO = Path(__file__).parent.parent / "files" / "panel.png"


class NavCb(CallbackData, prefix="nav"):
    screen: str


class ToggleCb(CallbackData, prefix="tgl"):
    field: str


class OffsetCb(CallbackData, prefix="off"):
    step: int


class TimeCb(CallbackData, prefix="time"):
    char: str


class SaveCb(CallbackData, prefix="save"):
    target: str


class ClearCb(CallbackData, prefix="clr"):
    pass


class CatCb(CallbackData, prefix="cat"):
    pass


class CloseCb(CallbackData, prefix="close"):
    pass


def _state_lines(chat_data: dict) -> str:
    """
    The two settings the chat has chosen, each on its own line under the cat
    """
    offset = chat_data.get("offset")
    time_str = str(chat_data.get("time") or "")
    format_24h = bool(chat_setting(chat_data, "format"))

    shown_offset = Placeholder.OFFSET if offset in (None, "") else format_offset(int(offset))
    shown_time = draft_display(time_str, format_24h)

    return f"{Icon.TIMEZONE} {shown_offset}\n{Icon.TIME} {shown_time}"


def _digit_key(char: str) -> InlineKeyboardButton:
    """
    One digit, one key · the character it shows is the character it sends
    """
    return InlineKeyboardButton(text=char, callback_data=TimeCb(char=char).pack())


def _offset_row(label: str, minutes: int) -> list[InlineKeyboardButton]:
    """
    A step down and the same step up · the label is what the key says, not what it moves
    """
    return [
        InlineKeyboardButton(
            text=f"{Sign.BUTTON_MINUS}{label}", callback_data=OffsetCb(step=-minutes).pack()
        ),
        InlineKeyboardButton(
            text=f"{Sign.BUTTON_PLUS}{label}", callback_data=OffsetCb(step=minutes).pack()
        ),
    ]


def _commit_row(target: str) -> list[InlineKeyboardButton]:
    """
    ✅ saves the draft, ↩️ leaves it · the row every entry screen ends with
    """
    return [
        InlineKeyboardButton(text=Icon.CONFIRM, callback_data=SaveCb(target=target).pack()),
        InlineKeyboardButton(text=Icon.BACK, callback_data=NavCb(screen="settings").pack()),
    ]


def build_main_screen() -> tuple[str, InlineKeyboardMarkup]:
    """
    The panel at rest: a cat to ask for, the settings, and the way out
    """
    text = Kaomoji.MAIN

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=Icon.CAT, callback_data=CatCb().pack()),
                InlineKeyboardButton(
                    text=Icon.SETTINGS, callback_data=NavCb(screen="settings").pack()
                ),
                InlineKeyboardButton(text=Icon.CLOSE, callback_data=CloseCb().pack()),
            ]
        ]
    )
    return text, kb


def build_settings_screen(chat_data: dict) -> tuple[str, InlineKeyboardMarkup]:
    """
    Three settings in one row · a toggle shows what is chosen, not what it would become
    """
    format_24h = chat_setting(chat_data, "format")

    text = f"{Kaomoji.SETTINGS}\n{_state_lines(chat_data)}"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=Icon.TIMEZONE, callback_data=NavCb(screen="offset").pack()
                ),
                InlineKeyboardButton(text=Icon.TIME, callback_data=NavCb(screen="time").pack()),
                InlineKeyboardButton(
                    text=Format.H24 if format_24h else Format.H12,
                    callback_data=ToggleCb(field="format").pack(),
                ),
            ],
            [InlineKeyboardButton(text=Icon.BACK, callback_data=NavCb(screen="main").pack())],
        ]
    )
    return text, kb


def build_offset_screen(draft_offset: int | None) -> tuple[str, InlineKeyboardMarkup]:
    """
    The offset moves by an hour or by a quarter · nothing is chosen until ✅
    """
    shown = Placeholder.OFFSET if draft_offset is None else format_offset(draft_offset)
    text = f"{Kaomoji.SETTINGS}\n{Icon.TIMEZONE} {shown}"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            _offset_row(Step.HOUR, 60),
            _offset_row(Step.MINUTE, 15),
            _commit_row("offset"),
        ]
    )
    return text, kb


def build_time_screen(draft_time: str, format_24h: bool) -> tuple[str, InlineKeyboardMarkup]:
    """
    Ten digits and a backspace · the twelve hour keypad carries the halves of the day too
    """
    text = f"{Kaomoji.SETTINGS}\n{Icon.TIME} {draft_display(draft_time, format_24h)}"

    halves = (
        []
        if format_24h
        else [
            InlineKeyboardButton(text=Half.FIRST, callback_data=TimeCb(char="am").pack()),
            InlineKeyboardButton(text=Half.SECOND, callback_data=TimeCb(char="pm").pack()),
        ]
    )

    rows = [
        [_digit_key(str(i)) for i in range(1, 6)],
        [_digit_key(str(i)) for i in [6, 7, 8, 9, 0]],
        [
            InlineKeyboardButton(text=Icon.BACKSPACE, callback_data=TimeCb(char="bksp").pack()),
            *halves,
            InlineKeyboardButton(text=Placeholder.TIME, callback_data=ClearCb().pack()),
        ],
        _commit_row("time"),
    ]

    return text, InlineKeyboardMarkup(inline_keyboard=rows)
