"""
Minimal Pyrogram bot for isolation testing.
Uses .env variables: API_ID, API_HASH, BOT_TOKEN
"""
import os
import logging
from dotenv import load_dotenv
from pyrogram import Client, filters

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(name)s | %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()

API_ID = int(os.getenv('API_ID', '0'))
API_HASH = os.getenv('API_HASH', '').strip()
BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()

app = Client(
    "testbot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

@app.on_message(filters.command("start"))
async def start_handler(client, message):
    logger.info(f"[START] from {getattr(message.from_user, 'id', None)}")
    await message.reply_text("Hello! (test bot)")

@app.on_message()
async def echo_handler(client, message):
    txt = getattr(message, 'text', '')
    logger.info(f"[MSG] {txt!r}")
    try:
        await message.reply_text(f"Echo: {txt}")
    except Exception as e:
        logger.error(f"echo error: {e}")

if __name__ == "__main__":
    app.run()


