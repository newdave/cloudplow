"""One --bwlimit for the running upload, combined from Plex throttling and system load.

Plex throttling and the load-based rate script both want to limit the upload. Each sets its own
rate here and the slower of the two is pushed to rclone, so neither overwrites the other: when Plex
streams stop, the upload falls back to the load limit rather than to unlimited.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional, Protocol

from . import misc

log = logging.getLogger(__name__)

UNLIMITED = 'off'


class Throttler(Protocol):
    def throttle(self, speed: str) -> bool: ...


class RateArbiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.plex_rate: Optional[str] = None
        self.load_rate: Optional[str] = None
        self.applied: str = UNLIMITED

    def start_upload(self, load_rate: Optional[str]) -> None:
        """A new rclone is starting with `load_rate` as its --bwlimit."""
        with self._lock:
            self.plex_rate = None
            self.load_rate = load_rate
            self.applied = load_rate or UNLIMITED

    def effective(self) -> str:
        plex, load = self.plex_rate, self.load_rate
        if plex and load:
            return misc.lower_rate(plex, load)
        return plex or load or UNLIMITED

    def set_plex(self, rate: Optional[str], throttler: Throttler, force: bool = False) -> bool:
        """`force` pushes the rate even if it looks applied, e.g. after a new rclone process started."""
        with self._lock:
            self.plex_rate = rate
            return self._sync(throttler, force)

    def set_load(self, rate: Optional[str], throttler: Throttler) -> bool:
        with self._lock:
            if rate != self.load_rate:
                log.info("Load-based upload limit changed from %s to %s", self.load_rate or UNLIMITED,
                         rate or UNLIMITED)
            self.load_rate = rate
            return self._sync(throttler)

    def _sync(self, throttler: Throttler, force: bool = False) -> bool:
        """Push the effective rate if rclone is not already running at it; True when they match."""
        wanted = self.effective()
        if wanted == self.applied and not force:
            return True
        if not throttler.throttle(wanted):
            return False
        log.info("Upload bandwidth limit is now %s (plex: %s, load: %s)", wanted, self.plex_rate or UNLIMITED,
                 self.load_rate or UNLIMITED)
        self.applied = wanted
        return True
