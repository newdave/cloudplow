from __future__ import annotations

import logging
import os
import re
import tempfile
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Tuple

from . import path
from .rclone import RcloneUploader
from .triggers import TriggerTracker

log = logging.getLogger(__name__)

RCLONE_MAX_TRANSFER_EXIT = 7
KILLED_EXIT = -9
EXCEPTION_EXIT = 9999
MAX_TRANSFER_SUSPEND_HOURS = 25


class FileIdentity(NamedTuple):
    """Enough of a stat() to tell whether a path still holds the exact file we copied."""
    device: int
    inode: int
    size: int
    mtime_ns: int


def file_identity(file_path: str) -> Optional[FileIdentity]:
    try:
        stat = os.stat(file_path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return FileIdentity(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


class VerifySummary(NamedTuple):
    removed: int = 0
    changed: int = 0
    not_copied: int = 0
    orphans_deleted: int = 0


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
        unmounted = [mount for mount in self.rclone_config.get('require_mounts', []) if not os.path.ismount(mount)]
        if unmounted:
            # an unmounted share is an empty directory on the root disk; never upload into it
            log.error("Upload to %s skipped because required mount(s) are not mounted: %s", self.name, unmounted)
            return UploadResult(0, f"Required mount(s) not mounted: {', '.join(unmounted)}", False)

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
        if rclone_config.get('verified_move'):
            ran, return_code = self._verified_move(rclone, triggers)
        else:
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
    def _verified_move(self, rclone: RcloneUploader, triggers: TriggerTracker) -> Tuple[bool, int]:
        """Copy, then remove each source only if it is provably the file that was copied.

        A plain `rclone move` deletes whatever is at the source path once its copy finishes. When
        the destination is also a mergerfs branch, an app working through the pool during that
        window can make that wrong: a file replaced at the same path loses its new version, and a
        file deleted or renamed mid-copy leaves a stale copy on the destination. Snapshotting each
        file's identity first and checking it again afterwards closes both races.
        """
        upload_folder = self.rclone_config['upload_folder']
        candidates = rclone.list_source()
        if candidates is None:
            log.error("Unable to list %r for upload", upload_folder)
            return False, EXCEPTION_EXIT

        snapshot: Dict[str, FileIdentity] = {}
        for relative_path in candidates:
            identity = file_identity(os.path.join(upload_folder, relative_path))
            if identity is not None:
                snapshot[relative_path] = identity
        if not snapshot:
            log.info("Nothing to upload from %r", upload_folder)
            return True, 0

        with tempfile.NamedTemporaryFile('w', prefix='cloudplow-', suffix='.files', encoding='utf-8') as files_from:
            files_from.write(''.join(f'{relative_path}\n' for relative_path in sorted(snapshot)))
            files_from.flush()
            ran, return_code = rclone.upload(triggers, command='copy', files_from=files_from.name)
            copied = rclone.list_destination(files_from.name)

        if copied is None:
            log.error("Unable to verify the upload to %s; leaving every source file in place", self.name)
            return ran, return_code if return_code else EXCEPTION_EXIT

        summary = self._reconcile(snapshot, copied, rclone)
        log.info("Verified move for %s: %d moved, %d changed during upload (kept), %d not copied (kept), "
                 "%d stale destination copies deleted", self.name, *summary)
        return ran, return_code

    def _reconcile(self, snapshot: Mapping[str, FileIdentity], copied: Mapping[str, int],
                   rclone: RcloneUploader) -> VerifySummary:
        upload_folder = self.rclone_config['upload_folder']
        removed = changed = not_copied = orphans = 0
        for relative_path, before in sorted(snapshot.items()):
            source = os.path.join(upload_folder, relative_path)
            was_copied = copied.get(relative_path) == before.size
            now = file_identity(source)

            if now is None:
                # deleted or renamed through the pool mid-copy: our copy is stale, unless it never landed
                if was_copied:
                    log.warning("%r disappeared during upload; deleting its stale destination copy", relative_path)
                    orphans += rclone.delete_destination_file(relative_path)
                continue
            if now != before:
                log.info("%r changed during upload; keeping it for the next run", relative_path)
                changed += 1
            elif not was_copied:
                not_copied += 1
            elif self.dry_run:
                log.info("Dry run: would remove uploaded source %r", source)
            else:
                try:
                    os.remove(source)
                    removed += 1
                except OSError:
                    log.exception("Uploaded %r but failed to remove the source", source)
        return VerifySummary(removed, changed, not_copied, orphans)

    def _opened_files(self) -> List[str]:
        upload_folder = self.rclone_config['upload_folder']
        return [
            # rclone filters are relative to the upload folder; a leading / anchors them there
            item[len(upload_folder.rstrip('/')):]
            for item in path.opened_files(upload_folder, sudo=self.uploader_config.get('opened_files_sudo', False))
            if not self._is_opened_file_excluded(item)
        ]

    def _is_opened_file_excluded(self, file_path: str) -> bool:
        lowered = file_path.lower()
        return any(item.lower() in lowered for item in self.uploader_config.get('opened_excludes', []))
