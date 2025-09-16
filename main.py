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

# Initialize Pyrogram client
app = Client(
    "pdfbot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
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
    
    # Import handlers to register them
    try:
        # Core handlers
        import link_bot.core
        logger.info("✅ Core handlers loaded")
        
        # Batch handlers
        import link_bot.batch
        logger.info("✅ Batch handlers loaded")
        
        # Admin handlers
        import link_bot.admin
        logger.info("✅ Admin handlers loaded")
        
        # Downloader handlers
        import link_bot.downloaders.scribd
        logger.info("✅ Scribd downloader loaded")
        
        # Optional: Manga handlers if available
        try:
            from handlers.manga_handler import cmd_manga_start
            logger.info("✅ Manga handlers loaded")
        except ImportError:
            logger.info("ℹ️ Manga handlers not available")
        
    except Exception as e:
        logger.error(f"❌ Error loading handlers: {e}")
        sys.exit(1)
    
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
