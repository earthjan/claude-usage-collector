"""Small readers for single transcript records, shared by every stream."""
import re
from datetime import datetime, timedelta, timezone

SYNTHETIC_MODEL = "<synthetic>"
TIMESTAMP = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?"
    r"(Z|[+-]\d{2}:\d{2})?"
)


def is_compact_boundary(record: dict) -> bool:
    return record.get("type") == "system" and record.get("subtype") == "compact_boundary"


def usage_message(record: dict):
    if record.get("type") != "assistant":
        return None
    message = record.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
        return None
    if not isinstance(message.get("id"), str) or not message["id"]:
        return None
    if message.get("model") == SYNTHETIC_MODEL:
        return None
    return message


def attachment_of(record: dict, kind: str):
    if record.get("type") != "attachment":
        return None
    attachment = record.get("attachment")
    if not isinstance(attachment, dict) or attachment.get("type") != kind:
        return None
    return attachment


def count(value) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def format_ts(value, round_to_hour: bool):
    if not isinstance(value, str):
        return None
    match = TIMESTAMP.fullmatch(value)
    if match is None:
        return None
    try:
        moment = _to_utc(match)
    except (ValueError, OverflowError):
        return None
    if round_to_hour:
        moment = moment.replace(minute=0, second=0)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_utc(match) -> datetime:
    parts = [int(part) for part in match.groups()[:6]]
    moment = datetime(*parts, tzinfo=timezone.utc)
    zone = match.group(7)
    if zone and zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        hours, minutes = int(zone[1:3]), int(zone[4:6])
        moment -= sign * timedelta(hours=hours, minutes=minutes)
    return moment
