from __future__ import annotations

import functools
import logging
import timeit
from typing import Any, Callable, TypeVar

from . import misc

log = logging.getLogger("decorators")

F = TypeVar('F', bound=Callable[..., Any])


def timed(method: F) -> F:
    @functools.wraps(method)
    def timer(*args: Any, **kwargs: Any) -> Any:
        start_time = timeit.default_timer()
        try:
            return method(*args, **kwargs)
        finally:
            log.info("%s finished in %s", method.__qualname__,
                     misc.seconds_to_string(timeit.default_timer() - start_time))

    return timer  # type: ignore[return-value]
