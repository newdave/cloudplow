from __future__ import annotations

import logging

import requests

log = logging.getLogger("pushover")


class Pushover:
    NAME = "Pushover"

    def __init__(self, app_token: str, user_token: str, priority: int = 0):
        self.app_token = app_token
        self.user_token = user_token
        self.priority = priority
        log.info("Initialized Pushover notification agent")

    def send(self, message: str) -> bool:
        if not self.app_token or not self.user_token:
            log.error("You must specify an app_token and user_token when initializing this class")
            return False
        payload = {'token': self.app_token, 'user': self.user_token, 'priority': self.priority, 'message': message}
        try:
            resp = requests.post('https://api.pushover.net/1/messages.json', data=payload, timeout=30)
            return resp.status_code == 200
        except requests.RequestException:
            log.exception("Error sending Pushover notification")
        return False
