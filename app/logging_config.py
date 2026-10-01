import logging
import sys

from loguru import logger

from app.settings import LOGS_DIR

LOG_FILE = LOGS_DIR / "bot.log"


class InterceptHandler(logging.Handler):
    """Redirect stdlib logging (used internally by aiogram/aiohttp) into loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging() -> None:
    """Configure loguru: console output plus a rotating, persisted log file."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    logger.remove()
    logger.add(sys.stderr, level="INFO")
    logger.add(
        LOG_FILE,
        rotation="10 MB",
        retention="14 days",
        compression="zip",
        level="INFO",
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
