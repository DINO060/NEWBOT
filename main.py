"""
Main entry point for PDF Bot
"""
import os
import sys
import asyncio
import logging
from pathlib import Path

from pyrogram import Client
from utils.database import db
from config import API_ID, API_HASH, BOT_TOKEN

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

# Initialize Pyrogram client with plugins
app = Client(
    "pdfbot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    plugins={"root": "link_bot"},
    in_memory=True
)

async def startup():
    """Startup tasks"""
    logger.info("🚀 Starting PDF Bot...")
    
    # Connect to MongoDB
    connected = await db.connect()
    if not connected:
        logger.error("❌ Failed to connect to MongoDB. Exiting...")
        sys.exit(1)
    
    logger.info("✅ MongoDB connected")
    
    # Handlers are auto-loaded via plugins
    logger.info("✅ Plugins loader registered: link_bot/*")
    
    # Start periodic cleanup task
    asyncio.create_task(cleanup_temp_files())
    logger.info("✅ Cleanup task started")
    
    logger.info("🟢 Bot is ready!")

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

def main():
    """Main function"""
    # Preflight env checks
    try:
        if not isinstance(API_ID, int) or API_ID <= 0:
            raise ValueError("Invalid API_ID (check .env)")
        if not API_HASH or len(API_HASH) < 20:
            raise ValueError("Invalid API_HASH (check .env)")
        if not BOT_TOKEN or ":" not in BOT_TOKEN:
            raise ValueError("Invalid BOT_TOKEN (check .env)")
        # Validate token via HTTPS getMe
        import json, urllib.request
        with urllib.request.urlopen(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
            if not data.get("ok"):
                raise RuntimeError(f"getMe failed: {data}")
            uname = data.get("result", {}).get("username")
            bid = data.get("result", {}).get("id")
            logger.info(f"🤖 Bot username: @{uname} (id={bid})")
    except Exception as e:
        logger.error(f"Preflight failed: {e}")
        return

    # Run startup tasks
    loop = asyncio.get_event_loop()
    loop.run_until_complete(startup())
    
    try:
        # Start the bot
        logger.info("🤖 Starting Pyrogram client...")
        app.run()
    except KeyboardInterrupt:
        logger.info("⛔ Bot stopped by user")
    finally:
        # Run shutdown tasks
        loop.run_until_complete(shutdown())

if __name__ == "__main__":
    main()
