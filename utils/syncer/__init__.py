from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional

from .local import Local
from .scaleway import Scaleway

log = logging.getLogger("syncer")

SERVICES = {
    'scaleway': Scaleway,
    'local': Local
}


class Syncer:
    """Registry of configured sync agents, keyed by syncer name."""

    def __init__(self, config: Mapping[str, Any]):
        self.config = config
        self.agents: Dict[str, Any] = {}

    def load(self, syncer_name: str, service: Optional[str] = None, tool_path: Optional[str] = None,
             sync_from: Optional[str] = None, sync_to: Optional[str] = None, **kwargs: Any) -> bool:
        if service not in SERVICES:
            log.error("You specified an invalid sync service to load for %s: %s", syncer_name, service)
            return False
        if not tool_path:
            log.error("You must specify a tool_path for syncer %s", syncer_name)
            return False
        if not sync_from or not sync_to:
            log.error("You must specify a sync_from and sync_to for syncer %s", syncer_name)
            return False

        try:
            remotes = self.config['remotes']
            self.agents[syncer_name] = SERVICES[service](tool_path, remotes[sync_from], remotes[sync_to],
                                                         syncer_name=syncer_name, **kwargs)
            return True
        except KeyError as missing:
            log.error("Syncer %s refers to remote %s which is not in the remotes config", syncer_name, missing)
        return False

    def get(self, syncer_name: str) -> Optional[Any]:
        agent = self.agents.get(syncer_name)
        if agent is None:
            log.error("No sync agent was loaded for syncer: %s", syncer_name)
        return agent
