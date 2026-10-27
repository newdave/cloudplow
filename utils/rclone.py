from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urljoin

import requests

from . import process
from .triggers import TriggerTracker

log = logging.getLogger(__name__)

# rclone filter flags (prefixes); these cannot be combined with --files-from-raw
FILTER_FLAGS = ('--exclude', '--include', '--filter', '--files-from', '--min-age', '--max-age', '--min-size',
                '--max-size', '--ignore-case', '--metadata-exclude', '--metadata-include', '--metadata-filter',
                '--hash-filter', '--max-depth')

# Remote types whose single upstream is stored under the 'remote' key
_WRAPPING_REMOTE_TYPES = ('crypt', 'chunker', 'alias')


def extras_to_args(extras: Mapping[str, Any]) -> List[str]:
    """Convert an `rclone_extras` mapping into CLI flags; a None value yields a bare flag."""
    return [key if value is None else f'{key}={value}' for key, value in extras.items()]


def excludes_to_args(excludes: Sequence[str]) -> List[str]:
    return [f'--exclude={pattern}' for pattern in excludes]


def rc_args(plex: Optional[Mapping[str, Any]]) -> List[str]:
    """Enable rclone's remote-control API when Plex throttling is configured."""
    if not plex or not plex.get('enabled'):
        return []
    rc_addr = re.sub(r'https?://(www\.)?', '', plex['rclone']['url']).strip().strip('/')
    return ['--rc', '--rc-no-auth', f'--rc-addr={rc_addr}']


def join_remote(base: str, relative_path: str) -> str:
    """Join a path onto an rclone location ('remote:', 'remote:/dir' or a local '/dir')."""
    if base.endswith(':'):
        return base + relative_path
    return f"{base.rstrip('/')}/{relative_path}"


def drive_upstreams(remotes: Mapping[str, Mapping[str, str]], name: str) -> List[str]:
    """Resolve the Google Drive remotes underlying `name` through crypt/chunker/alias/union layers."""
    if name not in remotes:
        raise ValueError(f'Remote {name!r} does not exist in the rclone config')

    remote = remotes[name]
    remote_type = remote['type']
    if remote_type == 'drive':
        return [name]
    if remote_type in _WRAPPING_REMOTE_TYPES:
        return drive_upstreams(remotes, remote['remote'].split(':')[0])
    if remote_type == 'union':
        upstreams = []
        for upstream in remote['upstreams'].split():
            upstreams.extend(drive_upstreams(remotes, upstream.split(':')[0]))
        return upstreams

    log.warning("%s has an unsupported type for service accounts: %s", name, remote_type)
    return []


class RcloneMover:
    def __init__(self, config: Mapping[str, Any], rclone_binary_path: str, rclone_config_path: str,
                 plex: Optional[Mapping[str, Any]], dry_run: bool = False):
        self.config = config
        self.rclone_binary_path = rclone_binary_path
        self.rclone_config_path = rclone_config_path
        self.plex = plex
        self.dry_run = dry_run

    def move(self) -> bool:
        source, destination = self.config['move_from_remote'], self.config['move_to_remote']
        log.debug("Moving %r to %r", source, destination)
        args = [self.rclone_binary_path, 'move', source, destination, f'--config={self.rclone_config_path}',
                *extras_to_args(self.config.get('rclone_extras', {})),
                *excludes_to_args(self.config.get('rclone_excludes', [])),
                *rc_args(self.plex)]
        if self.dry_run:
            args.append('--dry-run')

        try:
            return process.stream(args, logger=log).returncode == 0
        except OSError:
            log.exception("Exception occurred while moving %r to %r:", source, destination)
        return False


class RcloneUploader:
    def __init__(self, name: str, config: Mapping[str, Any], rclone_binary_path: str, rclone_config_path: str,
                 plex: Optional[Mapping[str, Any]] = None, dry_run: bool = False,
                 service_account: Optional[str] = None):
        self.name = name
        self.config = config
        self.rclone_binary_path = rclone_binary_path
        self.rclone_config_path = rclone_config_path
        self.plex = plex
        self.dry_run = dry_run
        self.service_account = service_account

    def delete_file(self, path: str) -> bool:
        log.debug("Deleting file %r from remote %s", path, self.name)
        return self._run_delete('delete', path, 'Failed to delete')

    def delete_folder(self, path: str) -> bool:
        log.debug("Deleting folder %r from remote %s", path, self.name)
        return self._run_delete('rmdir', path, 'Failed to rmdir')

    def upload(self, callback: Callable[[str], bool], command: Optional[str] = None,
               files_from: Optional[str] = None) -> Tuple[bool, int]:
        """Run the configured move/copy; returns (ran, rclone exit code).

        `command` overrides the configured rclone command; `files_from` restricts the transfer
        to the paths (relative to upload_folder) listed in that file.
        """
        command = command or self.config.get('rclone_command', 'move')
        if command.lower() == 'sync':
            # sync would delete remote files missing locally, never allow it for uploads
            command = 'move'
        log.debug("Uploading %r to %r using rclone %s", self.config['upload_folder'],
                  self.config['upload_remote'], command)

        args = [self.rclone_binary_path, command, self.config['upload_folder'], self.config['upload_remote'],
                f'--config={self.rclone_config_path}', *rc_args(self.plex)]
        if files_from:
            # rclone rejects --files-from-raw combined with any other filter; the list is already filtered
            args += [*self._transfer_args(), f'--files-from-raw={files_from}', '--no-traverse']
        else:
            args += self._filter_args()
        if self.dry_run:
            args.append('--dry-run')

        try:
            env = self._service_account_env() if self.service_account else None
            return True, process.stream(args, callback, env, logger=log).returncode
        except (OSError, ValueError):
            log.exception("Exception occurred while uploading %r to remote: %s", self.config['upload_folder'],
                          self.name)
        return False, 9999

    def list_source(self) -> Optional[Dict[str, int]]:
        """Files rclone would consider for upload (after excludes/age filters): {relative path: size}."""
        return self._list(self.config['upload_folder'], self._filter_args())

    def list_destination(self, files_from: str) -> Optional[Dict[str, int]]:
        """Sizes of the given relative paths at the upload destination (missing paths are omitted)."""
        return self._list(self.config['upload_remote'], [f'--files-from-raw={files_from}'])

    def delete_destination_file(self, relative_path: str) -> bool:
        target = join_remote(self.config['upload_remote'], relative_path)
        if self.dry_run:
            log.info("Dry run: would delete %r", target)
            return True
        result = process.run([self.rclone_binary_path, 'deletefile', target, f'--config={self.rclone_config_path}'])
        if result.returncode != 0:
            log.error("Failed deleting %r: %s", target, result.output)
        return result.returncode == 0

    # internals
    def _filter_args(self) -> List[str]:
        return [*extras_to_args(self.config.get('rclone_extras', {})),
                *excludes_to_args(self.config.get('rclone_excludes', []))]

    def _transfer_args(self) -> List[str]:
        """rclone_extras without filter flags, for transfers driven by an explicit file list."""
        extras = self.config.get('rclone_extras', {})
        return extras_to_args({key: value for key, value in extras.items() if not key.startswith(FILTER_FLAGS)})

    def _list(self, location: str, extra_args: Sequence[str]) -> Optional[Dict[str, int]]:
        output = process.check_output([self.rclone_binary_path, 'lsjson', '-R', '--files-only', '--no-mimetype',
                                       '--no-modtime', location, f'--config={self.rclone_config_path}',
                                       *extra_args])
        if output is None:
            return None
        return {entry['Path']: entry['Size'] for entry in json.loads(output or '[]')}

    def _run_delete(self, command: str, path: str, failure_text: str) -> bool:
        args = [self.rclone_binary_path, command, path, f'--config={self.rclone_config_path}']
        if self.dry_run:
            args.append('--dry-run')
        try:
            result = process.run(args)
            return result.returncode == 0 and failure_text not in result.output
        except OSError:
            log.exception("Exception running rclone %s on %r for remote %s: ", command, path, self.name)
        return False

    def _service_account_env(self) -> Dict[str, str]:
        dump = process.check_output([self.rclone_binary_path, 'config', 'dump',
                                     f'--config={self.rclone_config_path}'])
        if dump is None:
            raise ValueError('Unable to dump the rclone config')

        remote_name = self.config['upload_remote'].split(':')[0]
        upstreams = list(dict.fromkeys(drive_upstreams(json.loads(dump), remote_name)))
        log.debug("Parsed remotes: %s", upstreams)
        if not upstreams:
            log.warning('No remotes were added to ENV.')

        env = os.environ.copy()
        for upstream in upstreams:
            env[f'RCLONE_CONFIG_{upstream.upper()}_SERVICE_ACCOUNT_FILE'] = self.service_account
        return env


class RcloneSyncer:
    def __init__(self, from_remote: Mapping[str, Any], to_remote: Mapping[str, Any], **kwargs: Any):
        self.from_config = from_remote
        self.to_config = to_remote
        self.triggers = TriggerTracker({**from_remote['rclone_sleeps'], **to_remote['rclone_sleeps']})
        self.rclone_extras = kwargs.get('rclone_extras', {})
        self.dry_run = kwargs.get('dry_run', False)
        self.use_copy = kwargs.get('use_copy', False)

    def sync(self, cmd_wrapper: Callable[[List[str]], List[str]]) -> Tuple[bool, int, Optional[str]]:
        """Run the sync through `cmd_wrapper`; returns (success, suspend hours, tripped trigger)."""
        args = ['rclone', 'copy' if self.use_copy else 'sync', self.from_config['sync_remote'],
                self.to_config['sync_remote'], *extras_to_args(self.rclone_extras)]
        if self.dry_run:
            args.append('--dry-run')

        process.stream(cmd_wrapper(args), self.triggers, logger=log)
        return not self.triggers.delay_hours, self.triggers.delay_hours, self.triggers.tripped


class RcloneThrottler:
    """Adjusts the bandwidth limit of a running rclone through its remote-control API."""

    def __init__(self, url: str):
        self.url = url

    def validate(self) -> bool:
        data = self._post('rc/noop', {'validated': True})
        return bool(data and data.get('validated'))

    def throttle_active(self, speed: Optional[str]) -> bool:
        if not speed:
            return False
        data = self._post('core/stats')
        transferring = (data or {}).get('transferring') or []
        if not transferring:
            return False
        # throttle is considered active when total speed is within 10 MB/s of the limit
        current_speed = sum(float(transfer['speed']) for transfer in transferring)
        return current_speed / 1000000 - 10 <= float(speed.rstrip('M'))

    def throttle(self, speed: str) -> bool:
        data = self._post('core/bwlimit', {'rate': speed})
        if data and speed in data.get('rate', ''):
            log.warning("Successfully throttled %s to %s.", self.url, speed)
            return True
        log.error("Failed to throttle %s to %s: %s", self.url, speed, data)
        return False

    def no_throttle(self) -> bool:
        data = self._post('core/bwlimit', {'rate': 'off'})
        if data and data.get('rate') == 'off':
            log.warning("Successfully un-throttled %s", self.url)
            return True
        log.error("Failed to un-throttle %s: %s", self.url, data)
        return False

    def _post(self, endpoint: str, payload: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        try:
            resp = requests.post(urljoin(self.url, endpoint), json=payload or {}, timeout=15)
            data = resp.json()
        except (requests.RequestException, ValueError):
            log.exception("Exception calling rclone rc endpoint %s at %s: ", endpoint, self.url)
            return None
        if 'error' in data:
            log.error("rclone rc endpoint %s returned an error: %s", endpoint, data['error'])
            return None
        return data
