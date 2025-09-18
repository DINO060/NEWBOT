"""
Main entry point for PDF Bot (Pyrogram + plugins) with robust startup/idle.
"""
import os
import sys
import asyncio
import logging
from pathlib import Path

from pyrogram import Client, idle, filters
from utils.database import db
from config import API_ID, API_HASH, BOT_TOKEN, ADMIN_IDS

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Create necessary directories
TEMP_DIR = Path("temp_files")
TEMP_DIR.mkdir(exist_ok=True)

BANNERS_DIR = Path("banners")
BANNERS_DIR.mkdir(exist_ok=True)

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

def _parse_admin_ids(raw: str):
    try:
        return [int(x) for x in str(raw or '').split(',') if x.strip()]
    except Exception:
        return []

# Initialize Pyrogram client with plugins (in-memory session to avoid local corruption)
app = Client(
    "pdfbot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# Explicitly import handlers to rule out plugin loader issues
from link_bot import core as _core_handlers  # noqa: F401
from link_bot import admin as _admin_handlers  # noqa: F401
from link_bot import batch as _batch_handlers  # noqa: F401
from link_bot import debug_echo as _debug_handlers  # noqa: F401

async def startup():
    """Startup tasks (after app.start())."""
    logger.info("🚀 Starting PDF Bot...")

    # DB connect
    if not await db.connect():
        logger.error("❌ Failed to connect to MongoDB. Exiting...")
        sys.exit(1)
    logger.info("✅ MongoDB connected")

    # Log identity via Bot API
    try:
        me = await app.get_me()
        logger.info(f"🤖 Bot username: @{getattr(me, 'username', None)} (id={getattr(me, 'id', None)})")
    except Exception as e:
        logger.error(f"get_me failed: {e}")

    logger.info("✅ Plugins loader registered: link_bot/*")

    # Sanity check handler registered locally
    @app.on_message(filters.command("ping") & filters.private)
    async def _local_ping(_, message):
        try:
            await message.reply_text("pong")
        except Exception as e:
            logger.error(f"local /ping failed: {e}")

    # Temporary local /start for sanity (bypasses plugin handlers)
    @app.on_message(filters.command("start") & filters.private)
    async def _local_start(_, message):
        try:
            await message.reply_text("✅ Start OK (local handler)")
        except Exception as e:
            logger.error(f"local /start failed: {e}")

    # Ping admins to verify outbound delivery
    admin_ids = _parse_admin_ids(ADMIN_IDS)
    for aid in admin_ids:
        try:
            await app.send_message(aid, "🟢 Bot démarré. Envoyez /ping ici pour tester la réception.")
            logger.info(f"Sent startup ping to admin {aid}")
        except Exception as e:
            logger.error(f"Startup ping failed for {aid}: {e}")

    # Start periodic cleanup
    asyncio.create_task(cleanup_temp_files())
    logger.info("✅ Cleanup task started")
    logger.info("🟢 Bot is ready! Send /start in DM.")

async def cleanup_temp_files():
    """Periodically clean temporary files"""
    while True:
        try:
            # Clean files older than 2 hours
            import time
            now = time.time()
            max_age = 2 * 60 * 60  # 2 hours
            
            for user_dir in TEMP_DIR.iterdir():
                if not user_dir.is_dir():
                    continue
                
                for file in user_dir.iterdir():
                    if file.is_file():
                        age = now - file.stat().st_mtime
                        if age > max_age:
                            try:
                                file.unlink()
                                logger.debug(f"Deleted old file: {file}")
                            except Exception:
                                pass
                
                # Remove empty directories
                try:
                    if not any(user_dir.iterdir()):
                        user_dir.rmdir()
                except Exception:
                    pass
                    
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        
        # Wait 10 minutes before next cleanup
        await asyncio.sleep(600)

async def shutdown():
    """Shutdown tasks"""
    logger.info("📴 Shutting down PDF Bot...")
    
    # Disconnect from MongoDB
    await db.disconnect()
    logger.info("✅ MongoDB disconnected")
    
    logger.info("👋 Goodbye!")

async def runner():
    # Basic env sanity
    if not isinstance(API_ID, int) or API_ID <= 0:
        raise SystemExit("🚫 Invalid API_ID")
    if not API_HASH or len(API_HASH) < 20:
        raise SystemExit("🚫 Invalid API_HASH")
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        raise SystemExit("🚫 Invalid BOT_TOKEN")

    await app.start()
    try:
        await startup()
        await idle()
    finally:
        await shutdown()
        await app.stop()

def main():
    asyncio.run(runner())

if __name__ == "__main__":
    main()
