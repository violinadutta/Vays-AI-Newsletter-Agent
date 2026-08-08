"""Configuration and cross-cutting infrastructure.

Import the two things you almost always need directly from here::

    from config import get_logger, get_settings

    settings = get_settings()
    log = get_logger(__name__)
"""

from config.logging_config import (
    bind_correlation_id,
    clear_context,
    configure_logging,
    get_logger,
    mask_email,
    new_correlation_id,
    set_log_level,
)
from config.settings import Settings, get_settings

__all__ = [
    "Settings",
    "bind_correlation_id",
    "clear_context",
    "configure_logging",
    "set_log_level",
    "get_logger",
    "get_settings",
    "mask_email",
    "new_correlation_id",
]
