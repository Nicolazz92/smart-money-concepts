"""Kill zone filter — check if a timestamp falls within active trading sessions."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

KILLZONES: dict[str, tuple[str, str]] = {
    "London open kill zone": ("06:00", "09:00"),
    "New York kill zone": ("11:00", "14:00"),
    "London close kill zone": ("14:00", "16:00"),
    "Asian kill zone": ("00:00", "04:00"),
    # MOEX main trading session: 10:00–18:00 MSK. Moscow has no DST, so
    # MSK = UTC+3 year-round, meaning the window in UTC is constant.
    "MOEX main session": ("07:00", "15:00"),
}


def _parse_time(t: str) -> time:
    h, m = t.split(":")
    return time(int(h), int(m))


def is_in_killzone(
    ts: datetime,
    active_killzones: list[str],
    timezone: str = "UTC",
) -> bool:
    """Return True if *ts* falls inside any of the active kill zones.

    Kill zone hours are defined in UTC.  The timestamp is converted to UTC
    before comparison so it works regardless of the provider's timezone.
    """
    if not active_killzones:
        return True  # no filter = always pass

    utc_time = ts.astimezone(ZoneInfo("UTC")).time() if ts.tzinfo else ts.time()

    for kz_name in active_killzones:
        bounds = KILLZONES.get(kz_name)
        if bounds is None:
            continue
        start = _parse_time(bounds[0])
        end = _parse_time(bounds[1])

        if start <= end:
            # Normal range (e.g. 06:00 - 09:00)
            if start <= utc_time < end:
                return True
        else:
            # Wraps midnight (e.g. 21:00 - 06:00)
            if utc_time >= start or utc_time < end:
                return True

    return False
