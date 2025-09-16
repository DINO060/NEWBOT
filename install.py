#!/usr/bin/env python3
"""
Installation and migration script for PDF Bot
Migrates from SQLite/JSON to MongoDB
"""
import os
import sys
import json
import sqlite3
import asyncio
from pathlib import Path
from datetime import datetime
import motor.motor_asyncio

# Color codes for terminal output
GREEN = '\033[92m'
YELLOW = '\033[93m'
RED = '\033[91m'
BLUE = '\033[94m'
RESET = '\033[0m'

def print_colored(text, color=RESET):
    try:
        print(f"{color}{text}{RESET}")
    except Exception:
        print(text)

def print_header():
    print_colored("""
╔══════════════════════════════════════════╗
║        PDF BOT INSTALLATION WIZARD        ║
║          SQLite/JSON → MongoDB           ║
╚══════════════════════════════════════════╝
    """, BLUE)

async def check_mongodb():
    """Check MongoDB connection"""
    print_colored("\n🔍 Checking MongoDB connection...", YELLOW)
    
    try:
        mongo_url = input("Enter MongoDB URL (default: mongodb://localhost:27017): ").strip()
    except EOFError:
        mongo_url = "mongodb://localhost:27017"
    if not mongo_url:
        mongo_url = "mongodb://localhost:27017"
    
    try:
        client = motor.motor_asyncio.AsyncIOMotorClient(mongo_url, serverSelectionTimeoutMS=5000)
        await client.server_info()
        print_colored("✅ MongoDB connection successful!", GREEN)
        return client
    except Exception as e:
        print_colored(f"❌ MongoDB connection failed: {e}", RED)
        return None

async def migrate_users(db, sqlite_file):
    """Migrate users from SQLite to MongoDB"""
    if not sqlite_file.exists():
        print_colored("⚠️ No SQLite database found, skipping users migration", YELLOW)
        return 0
    
    print_colored("\n📦 Migrating users...", YELLOW)
    
    conn = sqlite3.connect(sqlite_file)
    cursor = conn.cursor()
    
    try:
        cursor.execute("SELECT id FROM users")
        users = cursor.fetchall()
        
        if users:
            for user_id, in users:
                await db.users.update_one(
                    {'user_id': user_id},
                    {
                        '$set': {
                            'user_id': user_id,
                            'migrated_at': datetime.now()
                        }
                    },
                    upsert=True
                )
            
            print_colored(f"✅ Migrated {len(users)} users", GREEN)
            return len(users)
    except Exception as e:
        print_colored(f"⚠️ Error migrating users: {e}", YELLOW)
        return 0
    finally:
        conn.close()

async def migrate_stats(db, sqlite_file):
    """Migrate statistics from SQLite to MongoDB"""
    if not sqlite_file.exists():
        print_colored("⚠️ No SQLite database found, skipping stats migration", YELLOW)
        return
    
    print_colored("\n📊 Migrating statistics...", YELLOW)
    
    conn = sqlite3.connect(sqlite_file)
    cursor = conn.cursor()
    
    try:
        cursor.execute("SELECT files, storage_bytes FROM stats WHERE id=1")
        stats = cursor.fetchone()
        
        if stats:
            files, storage_bytes = stats
            await db.stats.update_one(
                {'_id': 'global'},
                {
                    '$set': {
                        'files': files,
                        'storage_bytes': storage_bytes,
                        'migrated_at': datetime.now()
                    }
                },
                upsert=True
            )
            print_colored(f"✅ Migrated stats: {files} files, {storage_bytes} bytes", GREEN)
    except Exception as e:
        print_colored(f"⚠️ Error migrating stats: {e}", YELLOW)
    finally:
        conn.close()

async def migrate_json_settings(db):
    """Migrate JSON settings to MongoDB"""
    # Migrate PDF settings
    pdf_settings_file = Path("pdf_settings.json")
    if pdf_settings_file.exists():
        print_colored("\n⚙️ Migrating PDF settings...", YELLOW)
        
        try:
            with open(pdf_settings_file, "r", encoding="utf-8") as f:
                settings = json.load(f)
            
            for user_id, user_settings in settings.items():
                await db.user_settings.update_one(
                    {'user_id': int(user_id)},
                    {
                        '$set': {
                            **user_settings,
                            'migrated_at': datetime.now()
                        }
                    },
                    upsert=True
                )
            
            print_colored(f"✅ Migrated settings for {len(settings)} users", GREEN)
        except Exception as e:
            print_colored(f"⚠️ Error migrating PDF settings: {e}", YELLOW)
    
    # Migrate force join channels
    force_join_file = Path("force_join_channels.json")
    if force_join_file.exists():
        print_colored("\n📢 Migrating force join channels...", YELLOW)
        
        try:
            with open(force_join_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            channels = data.get('channels', [])
            if channels:
                await db.config.update_one(
                    {'_id': 'force_join'},
                    {
                        '$set': {
                            'channels': channels,
                            'migrated_at': datetime.now()
                        }
                    },
                    upsert=True
                )
                print_colored(f"✅ Migrated {len(channels)} force join channels", GREEN)
        except Exception as e:
            print_colored(f"⚠️ Error migrating force join channels: {e}", YELLOW)
    
    # Migrate usernames
    usernames_file = Path("usernames.json")
    if usernames_file.exists():
        print_colored("\n👤 Migrating usernames...", YELLOW)
        
        try:
            with open(usernames_file, "r", encoding="utf-8") as f:
                usernames = json.load(f)
            
            for user_id, username in usernames.items():
                await db.user_settings.update_one(
                    {'user_id': int(user_id)},
                    {
                        '$set': {
                            'username': username,
                            'migrated_at': datetime.now()
                        }
                    },
                    upsert=True
                )
            
            print_colored(f"✅ Migrated {len(usernames)} usernames", GREEN)
        except Exception as e:
            print_colored(f"⚠️ Error migrating usernames: {e}", YELLOW)

def create_env_file():
    """Create .env file with configuration"""
    print_colored("\n🔧 Creating configuration file...", YELLOW)
    
    env_path = Path(".env")
    if env_path.exists():
        try:
            overwrite = input("⚠️ .env file already exists. Overwrite? (y/n): ").strip().lower()
        except EOFError:
            overwrite = 'n'
        if overwrite != 'y':
            print_colored("Skipping .env creation", YELLOW)
            return
    
    print_colored("\nPlease enter your bot configuration:", BLUE)
    
    try:
        api_id = input("API_ID: ").strip()
        api_hash = input("API_HASH: ").strip()
        bot_token = input("BOT_TOKEN: ").strip()
        admin_ids = input("ADMIN_IDS (comma-separated): ").strip()
        mongo_url = input("MONGODB_URL (default: mongodb://localhost:27017): ").strip()
    except EOFError:
        api_id = ""
        api_hash = ""
        bot_token = ""
        admin_ids = ""
        mongo_url = "mongodb://localhost:27017"
    
    if not mongo_url:
        mongo_url = "mongodb://localhost:27017"
    
    env_content = f"""# PDF Bot Configuration
API_ID={api_id}
API_HASH={api_hash}
BOT_TOKEN={bot_token}
ADMIN_IDS={admin_ids}

# Database
MONGODB_URL={mongo_url}

# Optional settings
MAX_FILE_SIZE=2147483648
MAX_BATCH_FILES=24
AUTO_DELETE_DELAY=300
"""
    
    with open(env_path, "w", encoding="utf-8") as f:
        f.write(env_content)
    
    print_colored("✅ Configuration file created", GREEN)

def create_config_py():
    """Create config.py from environment variables"""
    print_colored("\n📝 Creating config.py...", YELLOW)
    
    config_content = '''"""
Bot configuration
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

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

# Paths
BASE_DIR = Path(__file__).parent
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
'''
    
    with open("config.py", "w", encoding="utf-8") as f:
        f.write(config_content)
    
    print_colored("✅ config.py created", GREEN)

async def main():
    """Main installation function"""
    print_header()
    
    # Check Python version
    if sys.version_info < (3, 8):
        print_colored("❌ Python 3.8+ is required", RED)
        sys.exit(1)
    
    print_colored("✅ Python version OK", GREEN)
    
    # Create necessary directories
    print_colored("\n📁 Creating directories...", YELLOW)
    for dir_name in ["link_bot", "utils", "temp_files", "banners", "data", "link_bot/downloaders"]:
        Path(dir_name).mkdir(exist_ok=True)
    print_colored("✅ Directories created", GREEN)
    
    # Create __init__.py files
    for dir_name in ["link_bot", "utils", "link_bot/downloaders"]:
        init_file = Path(dir_name) / "__init__.py"
        if not init_file.exists():
            init_file.write_text("")
    
    # Check MongoDB
    client = await check_mongodb()
    if not client:
        print_colored("\n❌ MongoDB is required. Please install and start MongoDB first.", RED)
        print_colored("Installation instructions: https://docs.mongodb.com/manual/installation/", YELLOW)
        sys.exit(1)
    
    db = client.pdfbot_database
    
    # Migrate data
    sqlite_file = Path("bot_data.sqlite3")
    
    if sqlite_file.exists() or Path("pdf_settings.json").exists() or Path("usernames.json").exists():
        print_colored("\n🔄 Starting data migration...", BLUE)
        
        # Migrate users and stats from SQLite
        if sqlite_file.exists():
            await migrate_users(db, sqlite_file)
            await migrate_stats(db, sqlite_file)
        
        # Migrate JSON settings
        await migrate_json_settings(db)
        
        print_colored("\n✅ Data migration completed!", GREEN)
    else:
        print_colored("\n⚠️ No existing data found to migrate", YELLOW)
    
    # Create configuration files
    create_env_file()
    create_config_py()
    
    # Install dependencies
    print_colored("\n📦 Installing Python dependencies...", YELLOW)
    os.system("pip install -r requirements.txt")
    
    # Install Playwright browsers
    print_colored("\n🌐 Installing Playwright browsers...", YELLOW)
    os.system("python -m playwright install chromium")
    
    print_colored("\n" + "="*50, GREEN)
    print_colored("✅ Installation completed successfully!", GREEN)
    print_colored("="*50, GREEN)
    
    print_colored("\n📚 Next steps:", BLUE)
    print_colored("1. Review and edit .env file if needed", YELLOW)
    print_colored("2. Copy your handler modules from the old project", YELLOW)
    print_colored("3. Run the bot with: python main.py", YELLOW)
    
    # Cleanup recommendation
    print_colored("\n🧹 Cleanup (optional):", BLUE)
    print_colored("After verifying the migration, you can remove:", YELLOW)
    print_colored("  - bot_data.sqlite3", YELLOW)
    print_colored("  - pdf_settings.json", YELLOW)
    print_colored("  - usernames.json", YELLOW)
    print_colored("  - force_join_channels.json", YELLOW)

if __name__ == "__main__":
    asyncio.run(main())
