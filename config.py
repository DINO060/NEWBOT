"""
Bot configuration
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Paths
BASE_DIR = Path(__file__).parent

# Load environment variables explicitly from project .env
load_dotenv(BASE_DIR / ".env")

# Telegram API
API_ID = int(os.getenv('API_ID', 0))
API_HASH = os.getenv('API_HASH', '')
BOT_TOKEN = os.getenv('BOT_TOKEN', '')

# Admin configuration
ADMIN_IDS = os.getenv('ADMIN_IDS', '')

# MongoDB
MONGODB_URL = os.getenv('MONGODB_URL', 'mongodb://localhost:27017')

# Bot settings
MAX_FILE_SIZE = int(os.getenv('MAX_FILE_SIZE', 2147483648))  # 2GB
MAX_BATCH_FILES = int(os.getenv('MAX_BATCH_FILES', 24))
AUTO_DELETE_DELAY = int(os.getenv('AUTO_DELETE_DELAY', 300))  # 5 minutes

# Directories
TEMP_DIR = BASE_DIR / "temp_files"
BANNERS_DIR = BASE_DIR / "banners"
DATA_DIR = BASE_DIR / "data"

# Messages
MESSAGES = {
    'start': """👋 Welcome to Advanced PDF Tools Bot!

Send me a PDF and I'll help you clean, edit, add banner and lock it.

📋 Features:
• Rename file (clean usernames)
• Unlock protected PDFs
• Remove pages
• Add your default banner
• Lock with your default password
• Batch processing

🎯 Commands:
/start - Show this message
/batch - Enable sequence mode
/process - Process sequence files
/setbanner - Set your default banner
/setpassword - Set default lock password
/status - Check bot status

📤 Just send me a PDF to get started!""",
    'not_pdf': "❌ This is not a PDF file!",
    'file_too_big': "❌ File is too large!",
    'processing': "⏳ Processing...",
    'success_unlock': "✅ PDF unlocked successfully!",
    'success_pages': "✅ Pages removed successfully!",
    'error': "❌ Error during processing"
}
