from __future__ import annotations

import logging
import shlex
import time
from typing import Any, List, Mapping, Optional, Tuple

from utils import process
from utils.rclone import RcloneSyncer

log = logging.getLogger(__name__)

BOOT_WAIT_SECONDS = 60
INSTALL_UNZIP = "apt-get -qq update && apt-get -y -qq install unzip && which unzip"
INSTALL_RCLONE = ("cd ~ && curl -sO https://downloads.rclone.org/rclone-current-linux-amd64.zip && "
                  "unzip -oq rclone-current-linux-amd64.zip && cd rclone-*-linux-amd64 && "
                  "cp -rf rclone /usr/bin/ && cd ~ && rm -rf rclone-* && chown root:root /usr/bin/rclone && "
                  "chmod 755 /usr/bin/rclone && mkdir -p /root/.config/rclone && which rclone")


REMOTE_RCLONE_CONFIG = '/root/.config/rclone/rclone.conf'


def _instance_listed(ps_output: str, name: str) -> bool:
    """Whether `scw ps` lists an instance called exactly `name` (not just a name containing it)."""
    wanted = name.lower()
    return any(wanted in (column.lower() for column in line.split()) for line in ps_output.splitlines()[1:])


class Scaleway:
    NAME = 'Scaleway'

    def __init__(self, tool_path: str, from_config: Mapping[str, Any], to_config: Mapping[str, Any],
                 **kwargs: Any):
        self.tool_path = tool_path
        self.sync_from_config = from_config
        self.sync_to_config = to_config
        self.kwargs = kwargs
        self.instance_id: Optional[str] = None
        self.region = kwargs.get('region', 'par1')
        self.type = kwargs.get('type', 'X64-2GB')
        self.image = kwargs.get('image', 'ubuntu-xenial')
        self.instance_destroy = kwargs.get('instance_destroy', True)
        self.syncer_name = kwargs.get('syncer_name', 'Unknown Syncer')

        log.info("Initialized Scaleway syncer agent for %s - %s -> %s using tool: %s", self.syncer_name,
                 from_config['sync_remote'], to_config['sync_remote'], tool_path)

    def startup(self, name: str) -> Tuple[bool, Optional[str]]:
        resp = self._scw('ps', '-a')
        if not resp or 'zone' not in resp.lower():
            log.error("Unexpected response while checking if instance %s exists: %s", name, resp)
            return False, self.instance_id

        if self.instance_destroy or not _instance_listed(resp, name):
            log.debug("Creating new instance...")
            resp = self._scw('run', '-d', f'--name={name}', '--ipv6', f'--commercial-type={self.type}', self.image)
            if not resp or 'failed' in resp.lower():
                log.error("Unexpected response while creating instance: %s", resp)
                return False, self.instance_id
            self.instance_id = resp
            log.info("Created new instance: %s", self.instance_id)
        else:
            log.debug("Starting instance...")
            resp = self._scw('start', name)
            if not resp or 'failed' in resp.lower():
                log.error("Unexpected response while starting instance: %s", resp)
                return False, name
            self.instance_id = name
            log.info("Started existing instance: %s", self.instance_id)

        log.info("Waiting for instance to finish booting...")
        time.sleep(BOOT_WAIT_SECONDS)
        resp = self._scw('exec', '-w', self.instance_id, 'uname -a')
        if not resp or 'gnu/linux' not in resp.lower():
            log.error("Unexpected response while waiting for instance to boot: %s", resp)
            self.destroy()
            return False, self.instance_id

        log.info("Instance has finished booting, uname: %s", resp)
        return True, self.instance_id

    def setup(self, rclone_config: str) -> bool:
        if not self.instance_id:
            log.error("Setup was called, but no instance_id was found, aborting...")
            return False

        resp = self._scw('exec', self.instance_id, INSTALL_UNZIP)
        if not resp or '/usr/bin/unzip' not in resp.lower():
            return self._fail(f"Unexpected response while installing unzip: {resp}")
        log.info("Installed unzip")

        resp = self._scw('exec', self.instance_id, INSTALL_RCLONE)
        if not resp or '/usr/bin/rclone' not in resp.lower():
            return self._fail(f"Unexpected response while installing rclone: {resp}")
        log.info("Installed rclone")

        # always as rclone.conf: the name the remote rclone reads, and the file sync() copies back
        resp = self._scw('cp', rclone_config, f'{self.instance_id}:{REMOTE_RCLONE_CONFIG}')
        if resp is None or len(resp) >= 2:
            return self._fail(f"Unexpected response while copying rclone config: {resp}")
        log.info("Copied across rclone.conf")

        log.info("Successfully setup instance: %s", self.instance_id)
        return True

    def destroy(self) -> bool:
        if not self.instance_id:
            log.error("Destroy was called, but no instance_id was found, aborting...")
            return False

        action = 'rm' if self.instance_destroy else 'stop'
        args = ('rm', '-f', self.instance_id) if self.instance_destroy else ('stop', self.instance_id)
        resp = self._scw(*args)
        if not resp or self.instance_id.lower() not in resp.lower():
            log.error("Unexpected response while running %s on instance %s: %s", action, self.instance_id, resp)
            return False

        log.info("%s instance: %s", 'Destroyed' if self.instance_destroy else 'Stopped', self.instance_id)
        return True

    def sync(self, rclone_config: str, **kwargs: Any) -> Tuple[bool, Optional[int], Optional[str]]:
        if not self.instance_id:
            log.error("Sync was called, but no instance_id was found, aborting...")
            return False, None, None

        rclone = RcloneSyncer(self.sync_from_config, self.sync_to_config, **{**kwargs, **self.kwargs})
        log.info("Starting sync for instance: %s", self.instance_id)
        resp, delayed_check, delayed_trigger = rclone.sync(self._wrap_command)
        log.info("Finished syncing for instance: %s", self.instance_id)

        # copy rclone.conf back from the instance in case refresh tokens were rotated
        remote_config = self._scw('exec', self.instance_id, f'cat {REMOTE_RCLONE_CONFIG}')
        if remote_config:
            with open(rclone_config, 'w') as fp:
                fp.write(remote_config + '\n')
            log.info("Copied rclone.conf from instance")
        else:
            log.error("Unexpected response while copying rclone config from instance: %s", remote_config)

        return resp, delayed_check, delayed_trigger

    # internals
    def _scw(self, *args: str) -> Optional[str]:
        return process.check_output([self.tool_path, f'--region={self.region}', *args])

    def _fail(self, message: str) -> bool:
        log.error(message)
        self.destroy()
        return False

    def _wrap_command(self, args: List[str]) -> List[str]:
        return [self.tool_path, f'--region={self.region}', 'exec', self.instance_id, shlex.join(args)]
