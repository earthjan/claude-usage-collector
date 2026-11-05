"""Cross-check periods: calendar months (YYYY-MM) and ISO weeks (YYYY-Www), in UTC."""
import calendar
import re
from datetime import date, timedelta

MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
WEEK = re.compile(r"(\d{4})-W(\d{2})")
MAX_AGE_DAYS = 21


def is_month(value) -> bool:
    return isinstance(value, str) and MONTH.fullmatch(value) is not None


def is_week(value) -> bool:
    found = WEEK.fullmatch(value) if isinstance(value, str) else None
    if found is None:
        return False
    try:
        date.fromisocalendar(int(found.group(1)), int(found.group(2)), 1)
    except ValueError:
        return False
    return True


def is_period(value) -> bool:
    return is_month(value) or is_week(value)


def bounds(period: str) -> tuple:
    """First and last UTC day of a valid month or week."""
    if is_week(period):
        start = date.fromisocalendar(int(period[:4]), int(period[6:]), 1)
        return start, start + timedelta(days=6)
    year, month = int(period[:4]), int(period[5:7])
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def week_of(day: date) -> str:
    year, week, _weekday = day.isocalendar()
    return "%04d-W%02d" % (year, week)


def previous_week(today: date) -> str:
    return week_of(today - timedelta(days=7))


def previous_month(today: date) -> str:
    if today.month == 1:
        return "%04d-12" % (today.year - 1)
    return "%04d-%02d" % (today.year, today.month - 1)


def due_weeks(last_checked, today: date) -> tuple:
    """Weeks to check after `last_checked`, oldest first, and the ones too old to check.

    A week whose Monday is more than MAX_AGE_DAYS before today is skipped,
    because its transcripts may already be deleted.
    """
    latest = bounds(previous_week(today))[0]
    if not is_week(last_checked):
        return [week_of(latest)], []
    due, skipped = [], []
    start = bounds(last_checked)[0] + timedelta(days=7)
    oldest_allowed = today - timedelta(days=MAX_AGE_DAYS)
    while start <= latest:
        (due if start >= oldest_allowed else skipped).append(week_of(start))
        start += timedelta(days=7)
    return due, skipped
