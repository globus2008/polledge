import logging
from pathlib import Path

from rich.logging import RichHandler


def setup_logging(log_dir: Path, verbose: bool = False) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)

    console = RichHandler(level=logging.DEBUG if verbose else logging.WARNING, show_path=False)
    root.addHandler(console)

    file_handler = logging.FileHandler(log_dir / "polledge.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(file_handler)

    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
