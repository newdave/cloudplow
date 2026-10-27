from __future__ import annotations

import logging
import os
from typing import Iterable, List, Optional

from . import process

log = logging.getLogger(__name__)


def find_items(folder: str, suffix: Optional[str] = None, depth: Optional[int] = None,
               files: bool = False) -> List[str]:
    """Find directories (or files, if `files` is set) below `folder` whose name ends with `suffix`.

    Results are ordered deepest-first so they can be removed in order.
    """
    found = []
    folder = folder.rstrip(os.sep) or os.sep   # os.walk keeps a trailing slash, which would skew the depth
    start_depth = folder.count(os.sep)
    for root, subdirs, filenames in os.walk(folder, topdown=True):
        if depth and root.count(os.sep) - start_depth >= depth:
            del subdirs[:]
            continue
        for name in (filenames if files else subdirs):
            if not suffix or name.lower().endswith(suffix.lower()):
                found.append(os.path.join(root, name))
    return sorted(found, key=lambda item: item.count(os.sep), reverse=True)


def opened_files(folder: str, sudo: bool = False) -> List[str]:
    """Return files below `folder` that are currently held open by a process visible to this host.

    Writes made through a mergerfs pool are held open by the mergerfs daemon (root), which an
    unprivileged lsof cannot see; `sudo` runs lsof via passwordless sudo so they are included.
    """
    # -F n emits machine-readable records; file names are the lines prefixed with 'n'
    result = process.run([*(['sudo', '-n'] if sudo else []), 'lsof', '-w', '-F', 'n', '+D', folder])
    names = (line[1:] for line in result.output.splitlines() if line.startswith('n'))
    return sorted({name for name in names if os.path.isfile(name)})


def get_size(folder: str, excludes: Optional[Iterable[str]] = None) -> int:
    """Size of `folder` in whole GiB as du reports it (rounded up); 0 if it cannot be measured.

    `excludes` are du --exclude patterns, matched against file and directory names.
    """
    args = ['du', '-s', '--block-size=1G', *(f'--exclude={pattern}' for pattern in excludes or []), folder]
    result = process.run(args)
    size = result.output.split('\t', 1)[0].strip() if result.returncode == 0 else ''
    if not size.isdigit():
        log.error("Unable to determine the size of %r: %s", folder, result.output)
        return 0
    return int(size)


def delete(paths: Iterable[str] | str) -> None:
    for item in [paths] if isinstance(paths, str) else paths:
        if not os.path.exists(item):
            log.debug("Skipping deletion of %r as it does not exist", item)
            continue
        log.debug("Removing %r", item)
        try:
            if os.path.isdir(item):
                os.rmdir(item)
            else:
                os.remove(item)
        except OSError:
            log.exception("Exception deleting %r: ", item)


def remove_empty_dirs(folder: str, depth: int) -> bool:
    if not os.path.exists(folder):
        log.error("Cannot remove empty directories from %r as it does not exist", folder)
        return False

    log.debug("Removing empty directories from %r with mindepth %d", folder, depth)
    result = process.run(['find', folder, '-mindepth', str(depth), '-type', 'd', '-empty', '-delete'])
    if result.returncode != 0:
        log.error("Failed removing empty directories from %r: %s", folder, result.output)
        return False
    return True
