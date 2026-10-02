import logging
import logging.handlers
import sys
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


class EventLogHandler(logging.Handler):
    """WARNING+ log records as dashboard events (the dashboard replaces the console log)."""

    def __init__(self, state):
        super().__init__(level=logging.WARNING)
        self.state = state

    def emit(self, record):
        try:
            level = "error" if record.levelno >= logging.ERROR else "warn"
            self.state.add_event(level, record.getMessage())
        except Exception:
            self.handleError(record)


def setup_logging(log_level: str = "INFO", console_handler: bool = True,
                  log_path: str = "arbitrage.log") -> logging.Logger:
    """File log rotated at midnight (14 days kept) + optional rich console log. Safe to call twice."""
    root_logger = logging.getLogger("ergo_arb")
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)
        h.close()
    root_logger.setLevel(logging.DEBUG)

    file_handler = logging.handlers.TimedRotatingFileHandler(log_path, when="midnight", backupCount=14,
                                                             encoding="utf-8")
    file_handler.setLevel(getattr(logging, log_level.upper()))
    file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s"))
    root_logger.addHandler(file_handler)

    if console_handler:
        rich_handler = RichHandler(console=console, show_time=True, show_path=False, markup=True,
                                   rich_tracebacks=True)
        rich_handler.setLevel(getattr(logging, log_level.upper()))
        root_logger.addHandler(rich_handler)
    return root_logger
