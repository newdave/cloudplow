from __future__ import annotations

import logging
import os
import sys

try:
    from git import GitCommandError, InvalidGitRepositoryError, Repo
except ImportError:
    sys.exit("You are missing the GitPython requirement or executing from the wrong working directory.")

log = logging.getLogger(__name__)


def check_version() -> None:
    """Log whether this checkout matches the latest commit on its origin."""
    try:
        repo = Repo(os.path.dirname(os.path.realpath(sys.argv[0])))
        branch = repo.active_branch.name
        current = str(repo.active_branch.commit)
        latest = str(repo.remotes.origin.fetch()[0].commit)
    except (InvalidGitRepositoryError, GitCommandError, AttributeError, IndexError, TypeError, ValueError):
        log.debug("Unable to check version due to failure to determine current/latest commits", exc_info=True)
        return

    if current != latest:
        log.debug("You are NOT using the latest %s: %s", branch, latest)
    else:
        log.debug("You are using the latest %s: %s", branch, latest)
