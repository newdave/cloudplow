from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class Sabnzbd:
    def __init__(self, url: str, apikey: str = ''):
        self.url = url.rstrip('/')
        self.apikey = apikey

    def pause_queue(self) -> bool:
        return self._request('pause')

    def resume_queue(self) -> bool:
        return self._request('resume')

    def _request(self, mode: str) -> bool:
        params = {'apikey': self.apikey, 'mode': mode, 'output': 'json'}
        try:
            resp = requests.get(f'{self.url}/api', params=params, timeout=15)
            resp.raise_for_status()
            return bool(resp.json().get('status'))
        except (requests.RequestException, ValueError):
            log.exception("Failed to %s the Sabnzbd queue", mode)
        return False
