from __future__ import annotations

import logging
import re
from typing import Any, List, Mapping, NamedTuple, Optional

from . import path
from .rclone import RcloneUploader
from .triggers import TriggerTracker

log = logging.getLogger("uploader")

RCLONE_MAX_TRANSFER_EXIT = 7
KILLED_EXIT = -9
EXCEPTION_EXIT = 9999
MAX_TRANSFER_SUSPEND_HOURS = 25


class UploadResult(NamedTuple):
    delay_hours: int
    trigger: str
    success: bool


def rclone_glob_escape(file_path: str) -> str:
    # rclone globs use backslash escapes; glob.escape's [[] form doesn't match in rclone
    return re.sub(r'([\\*?\[\]{}])', r'\\\1', file_path)


class Uploader:
    def __init__(self, name: str, uploader_config: Mapping[str, Any], rclone_config: Mapping[str, Any],
                 rclone_binary_path: str, rclone_config_path: str, plex: Mapping[str, Any], dry_run: bool):
        self.name = name
        self.uploader_config = uploader_config
        self.rclone_config = rclone_config
        self.rclone_binary_path = rclone_binary_path
        self.rclone_config_path = rclone_config_path
        self.plex = plex
        self.dry_run = dry_run
        self.service_account: Optional[str] = None

    def set_service_account(self, sa_file: str) -> None:
        self.service_account = sa_file
        log.info("Using service account: %s", sa_file)

    def upload(self) -> UploadResult:
        rclone_config = dict(self.rclone_config)
        rclone_config['rclone_excludes'] = list(rclone_config.get('rclone_excludes', []))

        if self.uploader_config.get('exclude_open_files'):
            open_files = self._opened_files()
            if open_files:
                log.info("Excluding these files from being uploaded because they were open: %s", open_files)
                rclone_config['rclone_excludes'].extend(rclone_glob_escape(item) for item in open_files)

        rclone = RcloneUploader(self.name, rclone_config, self.rclone_binary_path, self.rclone_config_path,
                                self.plex, self.dry_run, self.service_account)
        triggers = TriggerTracker(rclone_config.get('rclone_sleeps', {}))

        log.info("Uploading %r to remote: %s", rclone_config['upload_folder'], self.name)
        ran, return_code = rclone.upload(triggers)
        log.debug("return_code is: %s", return_code)

        if return_code == RCLONE_MAX_TRANSFER_EXIT:
            log.info("Received 'Max Transfer Reached' signal from Rclone.")
            return UploadResult(MAX_TRANSFER_SUSPEND_HOURS, "Rclone's 'Max Transfer Reached' signal", True)
        if return_code == KILLED_EXIT and triggers.tripped:
            log.info("Trigger reached configuration limit.")
            return UploadResult(triggers.delay_hours, triggers.tripped, True)
        if ran and return_code == 0:
            log.info("Finished uploading to remote: %s", self.name)
            return UploadResult(0, '', True)
        if return_code == EXCEPTION_EXIT:
            return UploadResult(0, 'Rclone exception occurred', False)
        return UploadResult(0, f"Unhandled situation: Exit code: {return_code} - Upload Status: {ran}", False)

    def remove_empty_dirs(self) -> None:
        folder, depth = self.rclone_config['upload_folder'], self.rclone_config['remove_empty_dir_depth']
        if path.remove_empty_dirs(folder, depth):
            log.info("Removed empty directories from %r with min depth: %d", folder, depth)

    # internals
    def _opened_files(self) -> List[str]:
        upload_folder = self.rclone_config['upload_folder']
        return [
            # rclone filters are relative to the upload folder; a leading / anchors them there
            item[len(upload_folder.rstrip('/')):]
            for item in path.opened_files(upload_folder)
            if not self._is_opened_file_excluded(item)
        ]

    def _is_opened_file_excluded(self, file_path: str) -> bool:
        lowered = file_path.lower()
        return any(item.lower() in lowered for item in self.uploader_config.get('opened_excludes', []))
