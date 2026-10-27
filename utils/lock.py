from __future__ import annotations

import fcntl
import logging
import os
import sys
from typing import IO, Optional

log = logging.getLogger("lock")
lock_folder = os.path.join(os.path.dirname(os.path.realpath(sys.argv[0])), 'locks')


class FileLock:
    """Exclusive inter-process lock backed by flock(2).

    The kernel releases the lock when the holder exits, so a crash never leaves a stale lock.
    Re-entrant use from different threads of one process is not supported; is_locked() is
    safe to call from any thread.
    """

    def __init__(self, path: str):
        self.path = path
        self._handle: Optional[IO[str]] = None

    def is_locked(self) -> bool:
        if self._handle is not None:
            return True
        with open(self.path, 'a') as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(probe, fcntl.LOCK_UN)
        return False

    def acquire(self) -> None:
        handle = open(self.path, 'a')
        fcntl.flock(handle, fcntl.LOCK_EX)
        self._handle = handle

    def release(self) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle, fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def ensure_lock_folder() -> None:
    try:
        os.makedirs(lock_folder, exist_ok=True)
    except OSError:
        log.exception("Exception verifying/creating lock folder %s: ", lock_folder)
        sys.exit(1)


def _lock(name: str) -> FileLock:
    return FileLock(os.path.join(lock_folder, f'{name}.flock'))


def upload() -> FileLock:
    return _lock('upload')


def sync() -> FileLock:
    return _lock('sync')


def hidden() -> FileLock:
    return _lock('hidden')
