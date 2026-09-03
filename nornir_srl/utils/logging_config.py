import logging
import os
import platform
import sys
from typing import Optional, List

from .. import __version__

_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# At DEBUG the useful part is usually where a line came from and which worker
# emitted it: reports fan out over a thread per node, so interleaved lines are
# only readable when each one names its thread and call site.
_DEBUG_FORMAT = (
    "%(asctime)s %(levelname)-7s %(name)s [%(threadName)s] "
    "%(funcName)s:%(lineno)d - %(message)s"
)

#: Dependencies that log a line per gRPC/HTTP frame at DEBUG, which buries
#: fcli's own tracing. ``FCLI_DEBUG_ALL`` lets them through as well.
_NOISY_LIBRARIES = (
    "anthropic",
    "asyncio",
    "grpc",
    "httpcore",
    "httpx",
    "markdown_it",
    "openai",
    "urllib3",
    "watchfiles",
)


def setup_logging(level: str, log_file: Optional[str] = None) -> None:
    """Configure basic logging.

    DEBUG also records the emitting thread and call site, and keeps the
    dependencies in :data:`_NOISY_LIBRARIES` at INFO unless ``FCLI_DEBUG_ALL``
    is set in the environment.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    verbose = numeric_level <= logging.DEBUG
    # stderr, so a DEBUG run does not corrupt the report on stdout: -o json|csv
    # stays pipeable while the trace is on screen.
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=numeric_level,
        format=_DEBUG_FORMAT if verbose else _FORMAT,
        handlers=handlers,
        force=True,
    )
    # ensure Nornir logs use the same level
    logging.getLogger("nornir").setLevel(numeric_level)
    # pygnmi attaches an unformatted StreamHandler to its own logger when it is
    # imported, and still propagates to the root one, so everything it logs
    # arrives twice: once bare and once through the format above.
    for name in ("pygnmi", "pygnmi.client"):
        pygnmi_logger = logging.getLogger(name)
        for handler in list(pygnmi_logger.handlers):
            pygnmi_logger.removeHandler(handler)

    if not verbose:
        return
    if not os.environ.get("FCLI_DEBUG_ALL"):
        for name in _NOISY_LIBRARIES:
            logging.getLogger(name).setLevel(logging.INFO)
    # Dates a log file and pins down the versions behind a bug report.
    logging.getLogger("nornir_srl").debug(
        "fcli %s on python %s (%s), pid %d, debug logging enabled%s",
        __version__,
        platform.python_version(),
        platform.platform(),
        os.getpid(),
        "" if os.environ.get("FCLI_DEBUG_ALL") else " (dependencies capped at INFO)",
    )
