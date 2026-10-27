from __future__ import annotations

import logging
import time
from typing import Dict, Mapping, Optional

log = logging.getLogger('triggers')


def _fmt(timestamp: float) -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timestamp))


class TriggerTracker:
    """Watches rclone output for configured `rclone_sleeps` phrases.

    Each trigger is `{count, timeout, sleep}`: when its phrase is seen `count` times within
    `timeout` seconds the run should be aborted and suspended for `sleep` hours.
    """

    def __init__(self, triggers: Mapping[str, Mapping[str, int]]):
        self.triggers = triggers
        self._counts: Dict[str, int] = {}
        self._expires: Dict[str, float] = {}
        self.delay_hours: int = 0
        self.tripped: Optional[str] = None

    def __call__(self, line: str) -> bool:
        """Process one output line; return True when the run should be aborted."""
        now = time.time()
        lowered = line.lower()
        for phrase, config in self.triggers.items():
            if phrase in self._expires and now >= self._expires[phrase]:
                log.warning("Tracking of trigger: %s has expired, resetting occurrence count and timeout", phrase)
                self._counts.pop(phrase)
                self._expires.pop(phrase)

            if phrase.lower() not in lowered:
                continue

            count = self._counts.get(phrase, 0) + 1
            self._counts[phrase] = count
            if count == 1:
                self._expires[phrase] = now + config['timeout']
                log.warning("Tracked first occurrence of trigger: %s. Expiring in %d seconds at %s",
                            phrase, config['timeout'], _fmt(self._expires[phrase]))
                continue

            log.warning("Tracked trigger: %s has occurred %d/%d times within %d seconds",
                        phrase, count, config['count'], config['timeout'])
            if count >= config['count']:
                log.warning("Tracked trigger %s has reached the maximum limit of %d occurrences within %d seconds, "
                            "aborting...", phrase, config['count'], config['timeout'])
                self.delay_hours = config['sleep']
                self.tripped = phrase
                return True
        return False
