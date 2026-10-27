from __future__ import annotations

import concurrent.futures
import logging
from typing import Any, List, Mapping, Optional, Tuple

from . import path
from .rclone import RcloneUploader

log = logging.getLogger('unionfs')

HIDDEN_SUFFIX = '_HIDDEN~'


class UnionfsHiddenFolder:
    def __init__(self, hidden_folder: str, dry_run: bool, rclone_binary_path: str, rclone_config_path: str):
        self.unionfs_fuse = hidden_folder
        self.dry_run = dry_run
        self.rclone_binary_path = rclone_binary_path
        self.rclone_config_path = rclone_config_path
        self.hidden_files = self._find(files=True)
        self.hidden_folders = self._find(files=False)

    def clean_remote(self, name: str, remote: Mapping[str, Any]) -> Tuple[bool, int, int]:
        """Delete hidden files and folders from the remote; returns (completed, deleted, failed)."""
        deleted = failed = 0
        rclone = RcloneUploader(name, remote, self.rclone_binary_path, self.rclone_config_path,
                                dry_run=self.dry_run)
        try:
            if self.hidden_files:
                log.info("Cleaning %d hidden file(s) from remote: %s", len(self.hidden_files), name)
                with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
                    futures = {}
                    for hidden_file in self.hidden_files:
                        remote_file = self._hidden2remote(remote, hidden_file)
                        if remote_file:
                            futures[executor.submit(rclone.delete_file, remote_file)] = remote_file
                        else:
                            failed += 1
                    for future in concurrent.futures.as_completed(futures):
                        if future.result():
                            log.info("Removed file %r", futures[future])
                            deleted += 1
                        else:
                            log.error("Failed removing file %r", futures[future])
                            failed += 1

            if self.hidden_folders:
                log.info("Cleaning %d hidden folder(s) from remote: %s", len(self.hidden_folders), name)
                for hidden_folder in self.hidden_folders:
                    remote_folder = self._hidden2remote(remote, hidden_folder)
                    if remote_folder and rclone.delete_folder(remote_folder):
                        log.info("Removed folder %r", remote_folder)
                        deleted += 1
                    else:
                        log.error("Failed removing folder %r", remote_folder)
                        failed += 1

            if self.hidden_folders or self.hidden_files:
                log.info("Completed cleaning hidden(s) from remote: %s. %d items were deleted, %d items failed",
                         name, deleted, failed)
            return True, deleted, failed

        except Exception:
            log.exception("Exception cleaning hidden(s) from %s: ", self.unionfs_fuse)
        return False, deleted, failed

    def remove_local_hidden(self) -> None:
        if self.hidden_files:
            path.delete(self.hidden_files)
            log.info("Removed %d local hidden file(s) from disk", len(self.hidden_files))
        if self.hidden_folders:
            path.delete(self.hidden_folders)
            log.info("Removed %d local hidden folder(s) from disk", len(self.hidden_folders))

    def remove_empty_dirs(self) -> None:
        if path.remove_empty_dirs(self.unionfs_fuse, 1):
            log.info("Removed empty directories from %r", self.unionfs_fuse)

    # internals
    def _find(self, files: bool) -> List[str]:
        kind = 'files' if files else 'folders'
        try:
            found = path.find_items(self.unionfs_fuse, HIDDEN_SUFFIX, files=files)
        except OSError:
            log.exception("Exception finding hidden %s for %s: ", kind, self.unionfs_fuse)
            return []
        log.info("Found %d hidden %s in %s", len(found), kind, self.unionfs_fuse)
        return found

    def _hidden2remote(self, remote: Mapping[str, Any], hidden_path: str) -> Optional[str]:
        if not remote.get('hidden_remote') or not hidden_path.startswith(self.unionfs_fuse):
            log.error("Failed mapping %r to a remote path", hidden_path)
            return None
        remote_path = remote['hidden_remote'] + hidden_path[len(self.unionfs_fuse):]
        if remote_path.endswith(HIDDEN_SUFFIX):
            remote_path = remote_path[:-len(HIDDEN_SUFFIX)]
        log.debug("Mapped %r to %r", hidden_path, remote_path)
        return remote_path
