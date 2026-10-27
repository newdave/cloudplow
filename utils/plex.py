from __future__ import annotations

import logging
import platform
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urljoin
from uuid import getnode

import requests

log = logging.getLogger('plex')

ACTIVE_STATES = ('playing', 'buffering')


class Plex:
    def __init__(self, url: str, token: str):
        self.url = url
        self.session = requests.Session()
        self.session.headers.update({
            'X-Plex-Token': token,
            'Accept': 'application/json',
            'X-Plex-Provides': 'controller',
            'X-Plex-Platform': platform.system(),
            'X-Plex-Platform-Version': platform.release(),
            'X-Plex-Product': 'cloudplow',
            'X-Plex-Version': '0.9.5',
            'X-Plex-Device': platform.platform(),
            'X-Plex-Client-Identifier': hex(getnode()),
        })

    def validate(self) -> bool:
        return self._sessions() is not None

    def get_streams(self) -> Optional[List[PlexStream]]:
        """Return current streams, an empty list if idle, or None if the server could not be queried."""
        result = self._sessions()
        if result is None:
            return None
        container = result.get('MediaContainer')
        if container is None:
            log.error("Failed to retrieve streams from server at %s", self.url)
            return None
        sessions = container.get('Video') or container.get('Metadata') or []
        if not sessions:
            log.debug("There were no streams to check for server at %s", self.url)
        return [PlexStream(stream) for stream in sessions]

    def _sessions(self) -> Optional[Dict[str, Any]]:
        request_url = urljoin(self.url, 'status/sessions')
        try:
            resp = self.session.get(request_url, timeout=15)
        except requests.RequestException:
            # the token lives in the session headers, so it is never part of this message
            log.exception("Exception retrieving sessions from %s: ", request_url)
            return None
        if resp.status_code != 200 or not resp.headers.get('Content-Type', '').startswith('application/json'):
            log.error("Server url or token was invalid, request_url=%s, status_code=%s", request_url,
                      resp.status_code)
            return None
        log.debug("Server responded with status_code=%s, content: %s", resp.status_code, resp.text)
        return resp.json()


class PlexStream:
    def __init__(self, stream: Mapping[str, Any]):
        player = stream.get('Player', {})
        self.user = stream.get('User', {}).get('title', 'Unknown')
        self.player = player.get('product', 'Unknown')
        self.state = player.get('state', 'Unknown')
        self.local = player.get('local')
        self.session_id = stream.get('Session', {}).get('id', 'Unknown')
        self.type = self.get_decision(stream.get('Media', []))

        if self.type == 'transcode':
            transcode = stream.get('TranscodeSession', {})
            self.video_decision = transcode.get('videoDecision', 'Unknown')
            self.audio_decision = transcode.get('audioDecision', 'Unknown')
        else:
            self.video_decision = self.audio_decision = 'directplay'

        if 'title' not in stream or 'type' not in stream:
            self.title = 'Unknown'
        elif stream['type'] == 'episode':
            self.title = f"{stream['grandparentTitle']} {stream['parentIndex']}x{stream['index']}"
        else:
            self.title = stream['title']

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES

    @staticmethod
    def get_decision(medias: List[Mapping[str, Any]]) -> str:
        for media in medias:
            for part in media.get('Part', []):
                if 'decision' in part:
                    return part['decision']
        return 'Unknown'

    def __str__(self) -> str:
        stream_type = self.type
        if self.type == 'transcode':
            transcoded = [kind for kind, decision in (('video', self.video_decision), ('audio', self.audio_decision))
                          if decision == 'transcode']
            stream_type = f"transcode ({' & '.join(transcoded)})"
        return (f"{self.user} is playing {self.title} using {self.player}. "
                f"Stream state: {self.state}, local: {self.local}, type: {stream_type}.")

    __repr__ = __str__
