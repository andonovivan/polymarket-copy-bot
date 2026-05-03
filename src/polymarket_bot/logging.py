"""Centralized structlog configuration."""

from __future__ import annotations

import logging

import structlog


def configure(level: str = "INFO") -> None:
    """Initialize structlog with a sane default for the bot."""
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(level),
        ),
    )
