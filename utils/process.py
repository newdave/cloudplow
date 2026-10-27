from __future__ import annotations

import logging
import subprocess
from typing import Callable, Mapping, NamedTuple, Optional, Sequence

log = logging.getLogger("process")

# Returned by the line callback; True requests that the process is killed.
LineCallback = Callable[[str], bool]


class Result(NamedTuple):
    returncode: int
    output: str


def stream(args: Sequence[str], callback: Optional[LineCallback] = None,
           env: Optional[Mapping[str, str]] = None, logs: bool = True) -> Result:
    """Run a command, handing every output line to `callback` as it arrives.

    stderr is merged into stdout. If the callback returns True the process is killed
    (returncode -9), but remaining buffered output is still drained.
    """
    log.debug("Executing: %s", subprocess.list2cmdline(args))
    lines = []
    with subprocess.Popen(list(args), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, errors='replace', bufsize=1) as proc:
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            lines.append(line)
            if logs:
                log.info(line)
            if callback and proc.poll() is None and callback(line):
                log.info("Callback requested termination, terminating...")
                proc.kill()
        returncode = proc.wait()

    return Result(returncode, '\n'.join(lines))


def run(args: Sequence[str], logs: bool = False) -> Result:
    """Run a command to completion and return its exit code and combined output."""
    return stream(args, logs=logs)


def check_output(args: Sequence[str]) -> Optional[str]:
    """Return stripped stdout of a successful command, or None if it failed to run or exited non-zero."""
    try:
        return subprocess.run(list(args), check=True, capture_output=True, text=True,
                              errors='replace').stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        log.exception("Exception while executing process: %s", subprocess.list2cmdline(args))
    return None
