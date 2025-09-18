"""
Debug echo plugin: replies to any private message to verify updates delivery.
Remove this file when debugging is done.
"""
import logging
from pyrogram import Client, filters

logger = logging.getLogger(__name__)

@Client.on_message(filters.private)
async def _echo_any_private(client, message):
    try:
        logger.info(f"[ECHO] from={message.from_user.id if message.from_user else None} text={getattr(message, 'text', None)!r}")
        await message.reply_text("✅ Received (debug echo)")
    except Exception as e:
        logger.error(f"echo error: {e}")


