"""Per-remote rclone flag overrides, written by a post_script and applied to the next upload.

A post_script cannot change an upload that has already finished, but it can leave a JSON file such
as {"--transfers": 2, "--bwlimit": "100M"} that is merged over the remote's rclone_extras the next
time that remote uploads. Only tuning flags are accepted so a script cannot change what is uploaded.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Mapping

log = logging.getLogger(__name__)

OVERRIDABLE_FLAGS = frozenset({
    '--bwlimit', '--transfers', '--checkers', '--tpslimit', '--multi-thread-streams', '--buffer-size',
})


def overrides_path(config_dir: str, remote: str) -> str:
    return os.path.join(config_dir, 'overrides', f'{remote}.json')


def load(file_path: str) -> Dict[str, Any]:
    """Return the accepted overrides in `file_path`; a missing or invalid file yields no overrides."""
    try:
        with open(file_path, encoding='utf-8') as fp:
            data = json.load(fp)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.warning("Ignoring unreadable rclone overrides file %r", file_path, exc_info=True)
        return {}
    if not isinstance(data, dict):
        log.warning("Ignoring rclone overrides file %r: expected a JSON object", file_path)
        return {}

    accepted = {flag: value for flag, value in data.items()
                if flag in OVERRIDABLE_FLAGS and isinstance(value, (int, str)) and not isinstance(value, bool)}
    ignored = sorted(set(data) - set(accepted))
    if ignored:
        log.warning("Ignoring unsupported entries in rclone overrides file %r: %s", file_path, ignored)
    return accepted


def apply(remote_config: Mapping[str, Any], overrides: Mapping[str, Any]) -> Dict[str, Any]:
    """A copy of `remote_config` with `overrides` merged over its rclone_extras."""
    return {**remote_config, 'rclone_extras': {**remote_config.get('rclone_extras', {}), **overrides}}
