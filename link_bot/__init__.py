"""
Plugin package initializer for Pyrogram.
Explicitly import submodules to ensure handler registration.
"""
import logging

logger = logging.getLogger(__name__)

try:
    from . import core  # noqa: F401
    from . import admin  # noqa: F401
    from . import batch  # noqa: F401
    from . import debug_echo  # noqa: F401 (can be removed in production)
    logger.info("✅ link_bot plugins loaded: core, admin, batch, debug_echo")
    print("✅ link_bot plugins loaded")
except Exception as e:
    logger.error(f"Error loading link_bot plugins: {e}")


