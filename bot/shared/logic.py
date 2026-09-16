"""
The rules the bot is made of · no clock, no store, no network, so the same
question always reads the same way
"""

import datetime
from enum import StrEnum


class Outcome(StrEnum):
    PHOTO = "photo"
    NO_LINKS = "no_links"
    BROKEN = "broken"
    RATE_LIMITED = "rate_limited"


def format_offset(offset_minutes: int) -> str:
    """
    How an offset reads: 0 · +3 · +5:45 · the minus is the typographic one, U+2212
    """
    if offset_minutes == 0:
        return "0"

    sign = "+" if offset_minutes > 0 else "−"
    abs_minutes = abs(offset_minutes)

    hours = abs_minutes // 60
    mins = abs_minutes % 60

    if mins == 0:
        return f"{sign}{hours}"
    return f"{sign}{hours}:{mins:02d}"


def format_time(hour: int, minute: int, format_24h: bool) -> str:
    """
    How a time reads · ¹ and ² stand for the halves of the day
    """
    if format_24h:
        return f"{hour}:{minute:02d}"

    period = "¹" if hour < 12 else "²"
    display_hour = hour % 12
    if display_hour == 0:
        display_hour = 12

    return f"{display_hour}:{minute:02d}{period}"


def is_a_time(hhmm: str) -> bool:
    """
    Four digits read as HH:MM
    """
    return len(hhmm) == 4 and hhmm.isdigit() and int(hhmm[:2]) <= 23 and int(hhmm[2:]) <= 59


def draft_reading(digits_draft: str) -> str | None:
    """
    What the draft reads as right now: four slots, a hyphen where no digit was typed.

    Digits enter from the left, one slot each, the way a clock is read aloud: "2" is the
    start of 2-:--, "23" is 23:--, "930" is 09:30 because a lone 9 can only be an hour's
    units. A trailing hyphen stands for a digit still to come, and a draft with no reading
    at all is one no later key could repair.
    """
    if not digits_draft:
        return "----"
    if not digits_draft.isdigit() or len(digits_draft) > 4:
        return None

    reading = digits_draft.ljust(4, "-")
    return reading if is_a_time(reading.replace("-", "0")) else None


def keeps_a_time(digits_draft: str) -> bool:
    """
    The draft is checked after every key · it is kept exactly while it still has a reading
    """
    return draft_reading(digits_draft) is not None


def draft_display(digits_draft: str, format_24h: bool) -> str:
    """
    How a draft reads on the screen · a trailing hyphen is the keypad saying a digit is
    still expected, and a draft with every slot filled reads the way a settled time does
    """
    reading = draft_reading(digits_draft) or "----"
    if reading.endswith("-"):
        return f"{reading[:2]}:{reading[2:]}"

    return format_time(int(reading[:2]), int(reading[2:]), format_24h)


def commit_draft(digits_draft: str) -> str | None:
    """
    What the screen shows, settled · a hyphen reads as a zero, whatever slot it sits in
    """
    if not digits_draft:
        return None
    reading = draft_reading(digits_draft)
    return reading.replace("-", "0") if reading else None


def offset_entry(current_offset_minutes: int, step_minutes: int) -> int:
    """
    The offset moves by a step and wraps · −12 hours is −720 minutes, +14 hours is 840
    """
    min_offset = -720
    max_offset = 840

    new_offset = current_offset_minutes + step_minutes
    if new_offset > max_offset:
        return min_offset
    if new_offset < min_offset:
        return max_offset

    return new_offset


def next_moment(
    local_time: datetime.time, offset_minutes: int, now_utc: datetime.datetime
) -> datetime.datetime:
    """
    Local time and offset give the next UTC instant, today or tomorrow
    """
    zone = datetime.timezone(datetime.timedelta(minutes=offset_minutes))
    local_now = now_utc.astimezone(zone)

    target_local = datetime.datetime.combine(local_now.date(), local_time, tzinfo=zone)
    target_utc = target_local.astimezone(datetime.UTC)

    if target_utc <= now_utc:
        target_utc += datetime.timedelta(days=1)

    return target_utc


def time_entry_append(digits_draft: str, new_digit: str) -> str:
    """
    Digits enter from the left, one slot each · a digit that cannot begin an hour settles
    it whole, so 9 is 09 and only 0, 1 and 2 wait for a second digit. A digit no slot can
    hold is refused and the draft is left as it was.
    """
    if len(digits_draft) >= 4:
        return digits_draft

    if not digits_draft:
        candidate = new_digit if new_digit in "012" else f"0{new_digit}"
    else:
        candidate = digits_draft + new_digit

    return candidate if draft_reading(candidate) is not None else digits_draft


def apply_half(digits_draft: str, half: str) -> str:
    """
    ¹ and ² move the hour into the first or the second half of the day · they read the
    draft the way ✅ would, so the half settles the digits nobody typed
    """
    committed = commit_draft(digits_draft)
    if committed is None:
        return digits_draft

    hour = int(committed[:2]) % 12
    if half == "pm":
        hour += 12

    return f"{hour:02d}{committed[2:]}"


def settled_schedule(
    chat_data: dict, now_utc: datetime.datetime
) -> tuple[int, str, float] | None:
    """
    A chat is scheduled only once it has both an offset and a time · a half chosen
    setting reads as nothing at all, and so does one no longer readable. The moment
    is counted from the instant the caller passes in
    """
    offset_raw = chat_data.get("offset")
    time_str = commit_draft(str(chat_data.get("time") or ""))
    if offset_raw in (None, "") or not time_str:
        return None

    try:
        offset_minutes = int(str(offset_raw))
        local_time = datetime.time(int(time_str[:2]), int(time_str[2:]))
    except ValueError:
        return None

    return offset_minutes, time_str, next_moment(local_time, offset_minutes, now_utc).timestamp()
