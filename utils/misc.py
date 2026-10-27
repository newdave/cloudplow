from __future__ import annotations

import re
import time
from typing import Iterable, List, Mapping, Optional, Tuple, TypeVar

T = TypeVar('T')


def get_lowest_remaining_time(data: Mapping[str, Optional[float]]) -> Optional[float]:
    """Return the earliest expiry among `data` values, or None if any entry is not banned."""
    values = list(data.values())
    if not values or any(value is None for value in values):
        return None
    return min(values)


def seconds_to_string(seconds: float) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = [f'{value} {unit}' for value, unit in
             ((days, 'days'), (hours, 'hours'), (minutes, 'minutes')) if value]
    if seconds or not parts:
        suffix = f'{seconds} seconds'
        return f"{', '.join(parts)} and {suffix}" if parts else suffix
    return ', '.join(parts)


_RATE = re.compile(r'^(\d+(?:\.\d+)?)([bkmgtp])?i?$', re.I)
_RATE_UNITS = 'bkmgtp'


def rate_to_bytes(rate: Optional[str]) -> Optional[float]:
    """Bytes/s for a simple rclone --bwlimit rate ('100M', '512k', '1G'); None for 'off' or timetables.

    Like rclone, a bare number is KiB/s and suffixes are binary multiples.
    """
    match = _RATE.match(str(rate).strip()) if rate is not None else None
    if not match:
        return None
    number, unit = match.groups()
    return float(number) * 1024 ** _RATE_UNITS.index((unit or 'k').lower())


def lower_rate(rate: str, cap: Optional[str]) -> str:
    """The slower of `rate` and `cap`; `rate` when either cannot be compared."""
    rate_bytes, cap_bytes = rate_to_bytes(rate), rate_to_bytes(cap)
    if rate_bytes is None or cap_bytes is None or cap is None:
        return rate
    return cap if cap_bytes < rate_bytes else rate


def get_nearest_less_element(data: Mapping[str, T], num: int) -> T:
    """Return the value whose integer key is the largest one <= num (or the smallest key if none are)."""
    keys = sorted(int(key) for key in data)
    eligible = [key for key in keys if key <= num]
    return data[str(eligible[-1] if eligible else keys[0])]


def is_time_between(time_range: Tuple[str, str], current_time: Optional[str] = None) -> bool:
    """Check whether HH:MM `current_time` falls within `time_range`, which may wrap past midnight."""
    start, end = time_range
    current_time = current_time or time.strftime('%H:%M')
    if end < start:
        return current_time >= start or current_time <= end
    return start <= current_time <= end


def sorted_naturally(items: Iterable[str]) -> List[str]:
    """Sort strings so embedded numbers compare numerically (sa2.json before sa10.json)."""
    return sorted(items, key=lambda item: [int(part) if part.isdigit() else part
                                           for part in re.split(r'(\d+)', item) if part])
