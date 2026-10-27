from __future__ import annotations

import logging
from typing import Any, List, Optional

from .apprise import Apprise
from .pushover import Pushover
from .slack import Slack

log = logging.getLogger(__name__)

SERVICES = {
    'apprise': Apprise,
    'pushover': Pushover,
    'slack': Slack
}


class Notifications:
    def __init__(self):
        self.services: List[Any] = []

    def load(self, service: Optional[str] = None, **kwargs: Any) -> bool:
        if service not in SERVICES:
            log.error("You specified an invalid notification service to load: %s", service)
            return False
        try:
            self.services.append(SERVICES[service](**kwargs))
            return True
        except TypeError:
            log.exception("Invalid settings for notification service %s: ", service)
        return False

    def send(self, message: str, service: Optional[str] = None) -> None:
        for agent in self.services:
            if service and agent.NAME.lower() != service.lower():
                continue
            try:
                if agent.send(message=message):
                    log.info("Sent notification with %s", agent.NAME)
            except Exception:
                log.exception("Exception sending notification with %s: ", agent.NAME)
