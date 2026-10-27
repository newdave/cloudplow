from __future__ import annotations

import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)


class Slack:
    NAME = "Slack"

    def __init__(self, webhook_url: str, sender_name: str = 'Cloudplow',
                 sender_icon: str = ':heavy_exclamation_mark:', channel: Optional[str] = None):
        self.webhook_url = webhook_url
        self.sender_name = sender_name
        self.sender_icon = sender_icon
        self.channel = channel
        log.info("Initialized Slack notification agent")

    def send(self, message: str) -> bool:
        if not self.webhook_url or not self.sender_name or not self.sender_icon:
            log.error("You must specify an webhook_url, sender_name and sender_icon when initializing this class")
            return False
        payload = {'text': message, 'username': self.sender_name, 'icon_emoji': self.sender_icon}
        if self.channel:
            payload['channel'] = self.channel
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=30)
            return resp.status_code == 200
        except requests.RequestException:
            # the webhook URL is a credential, keep it out of the log
            log.exception("Error sending Slack notification")
        return False
