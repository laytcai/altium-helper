"""Turn "yesterday", "3 days ago" or an ISO date into a time, in the local time zone."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

UNITS = {"minute": "minutes", "hour": "hours", "day": "days", "week": "weeks"}


def parse_when(text: str, now: datetime | None = None) -> datetime:
    now = (now or datetime.now()).astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    phrase = text.strip().lower()
    if phrase == "now":
        return now
    if phrase == "today":
        return midnight
    if phrase == "yesterday":
        return midnight - timedelta(days=1)
    if phrase == "last week":
        return midnight - timedelta(days=7)
    if match := re.fullmatch(r"(\d+)\s*(minute|hour|day|week)s?\s+ago", phrase):
        return now - timedelta(**{UNITS[match[2]]: int(match[1])})
    iso = text.strip()
    if iso.endswith(("Z", "z")):
        iso = iso[:-1] + "+00:00"  # Python 3.10 doesn't read the Z suffix
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        raise ValueError(
            f"Can't read the time {text!r}. Use an ISO date like 2026-09-29, "
            "or 'today', 'yesterday', 'last week', '3 days ago'."
        ) from None
    return when if when.tzinfo else when.astimezone()
