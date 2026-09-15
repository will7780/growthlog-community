"""
日志配置模块

统一配置：
- 日志格式：时间戳 + 级别 + 模块名 + 消息
- 输出：控制台 + 文件（带轮转）
- 文件路径：logs/app.log
- 默认 INFO；第三方 HTTP/SDK logger 不低于 WARNING，避免请求体泄漏
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

# SDK / HTTP clients may dump full request bodies (prompts, Authorization) at DEBUG.
NOISY_THIRD_PARTY_LOGGERS = (
    "openai",
    "httpx",
    "httpcore",
)


def _resolve_level(log_level: Optional[str], default: int = logging.INFO) -> int:
    if not log_level or not str(log_level).strip():
        return default
    return getattr(logging, str(log_level).strip().upper(), default)


def setup_logging(log_level: str = "INFO") -> int:
    """
    配置统一日志系统。

    Args:
        log_level: 有效日志级别，默认 INFO（不再默认 DEBUG）

    Returns:
        解析后的数值日志级别
    """
    level = _resolve_level(log_level, logging.INFO)

    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()

    console_handler = logging.StreamHandler()
    # Console never lower than INFO for readability; still respects higher configured levels.
    console_handler.setLevel(max(level, logging.INFO))
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        filename=str(log_dir / "app.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    # Follow configured effective level; do not hardcode DEBUG.
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    error_file_handler = RotatingFileHandler(
        filename=str(log_dir / "error.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    error_file_handler.setLevel(logging.ERROR)
    error_file_handler.setFormatter(formatter)
    root_logger.addHandler(error_file_handler)

    for name in NOISY_THIRD_PARTY_LOGGERS:
        noisy = logging.getLogger(name)
        noisy.setLevel(logging.WARNING)
        noisy.propagate = True

    logging.info("日志系统初始化完成 level=%s", logging.getLevelName(level))
    return level


def get_logger(name: str) -> logging.Logger:
    """获取指定名称的日志记录器。"""
    return logging.getLogger(name)
