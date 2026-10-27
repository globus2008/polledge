import logging
from pathlib import Path

from rich.logging import RichHandler


def setup_logging(log_dir: Path | None, verbose: bool = False, console: bool = True) -> None:
    """Console (rich) logging plus an optional log file; log_dir=None logs to the console only."""
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)

    if console:
        root.addHandler(_level(RichHandler(show_path=False), logging.DEBUG if verbose else logging.WARNING))

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / "polledge.log", encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(_level(file_handler, logging.DEBUG if verbose else logging.INFO))

    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _level(handler: logging.Handler, level: int) -> logging.Handler:
    handler.setLevel(level)
    return handler
