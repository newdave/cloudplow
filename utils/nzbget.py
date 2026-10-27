from __future__ import annotations

import logging
import xmlrpc.client

from .xmlrpc import server_proxy

log = logging.getLogger(__name__)


class Nzbget:
    def __init__(self, url: str):
        self.url = f"{url.rstrip('/')}/xmlrpc"

    def pause_queue(self) -> bool:
        return self._call('pausedownload')

    def resume_queue(self) -> bool:
        return self._call('resumedownload')

    def _call(self, method: str) -> bool:
        try:
            with server_proxy(self.url) as proxy:
                return bool(getattr(proxy, method)())
        except (OSError, xmlrpc.client.Error):
            log.exception("Exception calling NzbGet %s: ", method)
        return False
