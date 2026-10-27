"""Slow qBittorrent down during uploads with its alternative speed limits ("turtle mode").

The limits themselves are the ones set in qBittorrent (Options > Speed > Alternative Rate Limits). Cloudplow only
switches them on, and back off afterwards, if they were off to begin with; the fact that it switched them on is kept
in `state` so a restart mid-upload can still switch them off again.
"""
from __future__ import annotations

import logging
from typing import MutableMapping, Optional

import requests

log = logging.getLogger(__name__)

STATE_KEY = 'alt_limits_enabled'


class Qbittorrent:
    def __init__(self, url: str, username: str = '', password: str = '',
                 state: Optional[MutableMapping[str, bool]] = None):
        self.url = url.rstrip('/')
        self.username = username
        self.password = password
        self.state: MutableMapping[str, bool] = state if state is not None else {}
        self.session = requests.Session()
        # qBittorrent rejects API requests whose Referer/Origin does not match its own address
        self.session.headers['Referer'] = self.url

    # same interface as the Nzbget and Sabnzbd clients
    def pause_queue(self) -> bool:
        """Switch the alternative speed limits on; True if they are on afterwards."""
        mode = self._alt_limits_on()
        if mode is None:
            return False
        if mode:
            log.info("qBittorrent alternative speed limits are already on; leaving them as they are")
            return True
        if not self._toggle():
            return False
        self.state[STATE_KEY] = True
        return True

    def resume_queue(self) -> bool:
        """Switch the alternative speed limits off again, if cloudplow switched them on."""
        if not self.state.get(STATE_KEY):
            return True
        mode = self._alt_limits_on()
        if mode is None:
            return False
        if mode and not self._toggle():
            return False
        # also when someone already switched them off by hand: that choice stands
        self.state.pop(STATE_KEY, None)
        return True

    def restore_leftovers(self) -> bool:
        """Undo limits left on by a cloudplow that stopped mid-upload."""
        if self.state.get(STATE_KEY):
            log.info("Switching off qBittorrent alternative speed limits left on by an interrupted upload")
        return self.resume_queue()

    # internals
    def _alt_limits_on(self) -> Optional[bool]:
        text = self._request('get', 'transfer/speedLimitsMode')
        if text is None:
            return None
        if text.strip() not in ('0', '1'):
            log.error("Unexpected qBittorrent speed limits mode: %r", text)
            return None
        return text.strip() == '1'

    def _toggle(self) -> bool:
        return self._request('post', 'transfer/toggleSpeedLimitsMode') is not None

    def _login(self) -> bool:
        try:
            resp = self.session.post(f'{self.url}/api/v2/auth/login', timeout=15,
                                     data={'username': self.username, 'password': self.password})
            resp.raise_for_status()
        except requests.RequestException:
            log.exception("Failed to log in to qBittorrent at %s", self.url)
            return False
        if resp.text.strip() != 'Ok.':
            log.error("qBittorrent rejected the login for user %r", self.username)
            return False
        return True

    def _request(self, method: str, endpoint: str) -> Optional[str]:
        """Call the API, logging in first when needed; the response text, or None on failure."""
        url = f'{self.url}/api/v2/{endpoint}'
        try:
            resp = self.session.request(method, url, timeout=15)
            if resp.status_code == 403 and self.username:
                if not self._login():
                    return None
                resp = self.session.request(method, url, timeout=15)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException:
            log.exception("qBittorrent request %s failed", endpoint)
        return None
