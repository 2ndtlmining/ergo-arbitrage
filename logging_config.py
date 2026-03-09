import logging
import sys
from datetime import datetime
from rich.console import Console
from rich.logging import RichHandler
from rich.theme import Theme

custom_theme = Theme({
    "opportunity": "bold green",
    "no_opportunity": "dim white",
    "warning": "bold yellow",
    "error": "bold red",
    "price": "cyan",
    "profit": "bold green",
    "loss": "bold red",
    "header": "bold magenta",
    "exchange": "bold blue",
})

console = Console(theme=custom_theme)


def setup_logging(log_level: str = "INFO") -> logging.Logger:
    log_filename = f"arbitrage_{datetime.now().strftime('%Y%m%d')}.log"

    file_handler = logging.FileHandler(log_filename)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s")
    )

    rich_handler = RichHandler(
        console=console,
        show_time=True,
        show_path=False,
        markup=True,
        rich_tracebacks=True,
    )
    rich_handler.setLevel(getattr(logging, log_level.upper()))

    root_logger = logging.getLogger("ergo_arb")
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(rich_handler)

    return root_logger
