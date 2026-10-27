from __future__ import annotations

import logging

import apprise

log = logging.getLogger('apprise')


class Apprise:
    NAME = "Apprise"

    def __init__(self, url: str, title: str = 'Cloudplow'):
        self.url = url
        self.title = title
        log.debug("Initialized Apprise notification agent")

    def send(self, message: str) -> bool:
        if not self.url:
            log.error("You must specify a URL when initializing this class")
            return False
        apobj = apprise.Apprise()
        apobj.add(self.url)
        return bool(apobj.notify(title=self.title, body=message))
