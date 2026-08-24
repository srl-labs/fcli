import logging
import sys
from typing import Optional, List


def setup_logging(level: str, log_file: Optional[str] = None) -> None:
    """Configure basic logging."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
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
