from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable


class MemoryLogHandler(logging.Handler):
    def __init__(self, callback: Callable[[str], None] | None = None) -> None:
        super().__init__()
        self.callback = callback
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = self.format(record)
        self.messages.append(message)
        if self.callback:
            self.callback(message)


def build_logger(
    name: str = "road_report_app",
    log_file: str | Path | None = None,
    callback: Callable[[str], None] | None = None,
    level: int = logging.INFO,
) -> logging.Logger:
    logger = logging.getLogger(f"{name}_{id(callback)}_{log_file}")
    logger.setLevel(level)
    logger.propagate = False
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    memory_handler = MemoryLogHandler(callback=callback)
    memory_handler.setFormatter(formatter)
    logger.addHandler(memory_handler)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    logger.memory_handler = memory_handler  # type: ignore[attr-defined]
    return logger


def get_log_messages(logger: logging.Logger) -> list[str]:
    memory_handler = getattr(logger, "memory_handler", None)
    if memory_handler is None:
        return []
    return list(memory_handler.messages)
