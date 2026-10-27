from __future__ import annotations

import logging
import random
from typing import Any, List, Mapping, Optional, Tuple

from utils.rclone import RcloneSyncer

log = logging.getLogger(__name__)


class Local:
    NAME = 'Local'

    def __init__(self, tool_path: str, from_config: Mapping[str, Any], to_config: Mapping[str, Any],
                 **kwargs: Any):
        self.tool_path = tool_path
        self.sync_from_config = from_config
        self.sync_to_config = to_config
        self.kwargs = kwargs
        self.instance_id: Optional[int] = None
        self.rclone_config_path: Optional[str] = None
        self.syncer_name = kwargs.get('syncer_name', 'Unknown Syncer')
        self.instance_destroy = False

        log.info("Initialized Local syncer agent for %s - %s -> %s using tool: %s", self.syncer_name,
                 from_config['sync_remote'], to_config['sync_remote'], tool_path)

    def startup(self, name: str) -> Tuple[bool, Optional[int]]:
        # local syncs need no instance, use a placeholder id
        self.instance_id = random.randint(1, 10000)
        return True, self.instance_id

    def setup(self, rclone_config: str) -> bool:
        if not self.instance_id:
            log.error("Setup was called, but no instance_id was found, aborting...")
            return False
        self.rclone_config_path = rclone_config
        return True

    def destroy(self) -> bool:
        if not self.instance_id:
            log.error("Destroy was called, but no instance_id was found, aborting...")
            return False
        return True

    def sync(self, **kwargs: Any) -> Tuple[bool, Optional[int], Optional[str]]:
        if not self.instance_id:
            log.error("Sync was called, but no instance_id was found, aborting...")
            return False, None, None

        rclone = RcloneSyncer(self.sync_from_config, self.sync_to_config, **{**kwargs, **self.kwargs})
        log.info("Starting sync for instance: %s", self.instance_id)
        resp, delayed_check, delayed_trigger = rclone.sync(self._wrap_command)
        log.info("Finished syncing for instance: %s", self.instance_id)
        return resp, delayed_check, delayed_trigger

    # internals
    def _wrap_command(self, args: List[str]) -> List[str]:
        # swap the bare 'rclone' for the configured binary and point it at our config
        return [self.tool_path, *args[1:], f'--config={self.rclone_config_path}']
