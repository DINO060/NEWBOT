"""
Core PDF processing functions for PDF Bot
Handles individual PDF operations
"""
import os
import tempfile
import shutil
import logging
import asyncio
from pathlib import Path
from typing import Optional, List, Dict
from datetime import datetime

import pikepdf
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.enums import ParseMode
from PIL import Image
import fitz  # PyMuPDF

from utils.database import db
from utils.sessions import sessions, ensure_session_dict, set_processing_flag, clear_processing_flag
from utils.helpers import (
    build_final_filename,
    clean_caption_with_username,
    get_user_temp_dir,
    send_and_delete,
    is_pdf_file,
    parse_pages_spec,
    parse_pages_text,
    is_duplicate_message,
    send_limit_message
)
from link_bot.admin import is_user_in_channel, send_force_join_message
from link_bot.batch import user_batches, MAX_BATCH_FILES

logger = logging.getLogger(__name__)

# Banner storage
BANNERS_DIR = Path("banners")
BANNERS_DIR.mkdir(exist_ok=True)

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

def build_pdf_actions_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Build PDF actions keyboard"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Rename file", callback_data=f"rename_file:{user_id}")],
        [InlineKeyboardButton("🔓 Unlock", callback_data=f"unlock:{user_id}")],
        [InlineKeyboardButton("🗑️ Remove pages", callback_data=f"pages:{user_id}")],
        [InlineKeyboardButton("🛠️ The Both", callback_data=f"both:{user_id}")],
        [InlineKeyboardButton("⚡ Full Process", callback_data=f"fullproc:{user_id}")],
        [InlineKeyboardButton("🪧 Add banner", callback_data=f"add_banner:{user_id}")],
        [InlineKeyboardButton("🔐 Lock", callback_data=f"lock_now:{user_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{user_id}")],
    ])

async def send_welcome_message(client: Client, user_id: int):
    """Send welcome message with main menu"""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
        [InlineKeyboardButton("📦 Sequence Mode", callback_data="batch_mode")],
        [InlineKeyboardButton("🔗 Download Link", callback_data="download_link")]
    ])
    
    await client.send_message(user_id, MESSAGES['start'], reply_markup=keyboard)

@Client.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    """Handle /start command"""
    user_id = message.from_user.id
    
    # Track user
    await db.track_user(user_id)
    
    # Check force join
    if not await is_user_in_channel(client, user_id):
        await send_force_join_message(client, message)
        return
    
    # Check duplicate
    duplicate = is_duplicate_message(user_id, message.id, "start")
    if duplicate:
        if duplicate == "rate_limit":
            await send_limit_message(client, message.chat.id, "rate_limit")
        return
    
    # Initialize session
    session = ensure_session_dict(user_id)
    
    # Load saved settings from database
    settings = await db.get_user_settings(user_id)
    if settings.get('username'):
        session['username'] = settings['username']
    if settings.get('text_position'):
        session['text_position'] = settings['text_position']
    if settings.get('delete_delay'):
        session['delete_delay'] = settings['delete_delay']
    
    # Send welcome message
    await send_welcome_message(client, user_id)

@Client.on_message(filters.document & filters.private)
async def handle_document(client: Client, message: Message):
    """Handle document uploads"""
    user_id = message.from_user.id
    
    # Track user
    await db.track_user(user_id)
    
    # Initialize session
    session = ensure_session_dict(user_id)
    
    # Check if bot is processing
    if session.get('processing') and not session.get('batch_mode'):
        logger.info(f"Document ignored - processing in progress for user {user_id}")
        return
    
    # Check force join
    if not await is_user_in_channel(client, user_id):
        await send_force_join_message(client, message)
        return
    
    # Check duplicate
    if not session.get('batch_mode'):
        duplicate = is_duplicate_message(user_id, message.id, "document")
        if duplicate:
            if duplicate == "rate_limit":
                await send_limit_message(client, message.chat.id, "rate_limit")
            return
    
    doc = message.document
    if not doc:
        return
    
    # Check if it's a PDF
    if not is_pdf_file(doc.file_name or ""):
        await message.reply_text(MESSAGES['not_pdf'])
        return
    
    # Check file size
    MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
    if doc.file_size > MAX_FILE_SIZE:
        await message.reply_text(MESSAGES['file_too_big'])
        return
    
    file_id = doc.file_id
    file_name = doc.file_name or "document.pdf"
    
    # Check if in batch mode
    if session.get('batch_mode'):
        if user_id not in user_batches:
            user_batches[user_id] = []
        
        if len(user_batches[user_id]) >= MAX_BATCH_FILES:
            await message.reply_text(f"❌ Limit of {MAX_BATCH_FILES} files reached!")
            return
        
        # Add to batch
        file_info = {
            'file_id': file_id,
            'file_name': file_name,
            'is_video': False,
            'message_id': message.id,
            'size': doc.file_size
        }
        user_batches[user_id].append(file_info)
        
        # Save to database
        await db.add_batch_file(user_id, file_info)
        
        await message.reply_text(
            f"✅ **File added to batch** ({len(user_batches[user_id])}/{MAX_BATCH_FILES})\n\n"
            f"📄 {file_name}\n"
            f"📦 Size: {doc.file_size} bytes\n\n"
            f"Send `/process` when you're done adding files",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Normal mode - store file info
    session['file_id'] = file_id
    session['file_name'] = file_name
    
    # Show actions menu
    keyboard = build_pdf_actions_keyboard(user_id)
    await message.reply_text(
        f"📄 PDF received: {file_name}\n\nWhat do you want to do?",
        reply_markup=keyboard
    )

# PDF processing functions

async def _ensure_banner_pdf_path(user_id: int) -> Optional[str]:
    """Get user's banner PDF path, converting image to PDF if needed"""
    settings = await db.get_user_settings(user_id)
    banner_path = settings.get("banner_path")
    
    if not banner_path or not os.path.exists(banner_path):
        return None
    
    if banner_path.lower().endswith(".pdf"):
        return banner_path
    
    # Convert image to PDF
    try:
        out_pdf = BANNERS_DIR / (Path(banner_path).stem + ".pdf")
        with Image.open(banner_path) as im:
            if im.mode in ("RGBA", "P"):
                im = im.convert("RGB")
            im.save(out_pdf, "PDF", resolution=100.0)
        return str(out_pdf)
    except Exception as e:
        logger.error(f"Error converting banner to PDF: {e}")
        return None

async def add_banner_pages_to_pdf(in_pdf: str, out_pdf: str, banner_pdf: str, place: str = "after"):
    """Add banner pages to PDF"""
    try:
        with pikepdf.open(in_pdf) as pdf, pikepdf.open(banner_pdf) as banner:
            banner_pages = list(banner.pages)
            
            if place in ("before", "both", None, ""):
                for p in reversed(banner_pages):
                    pdf.pages.insert(0, p)
            
            if place in ("after", "both"):
                for p in banner_pages:
                    pdf.pages.append(p)
            
            pdf.save(out_pdf)
    except Exception as e:
        logger.error(f"Error adding banner: {e}")
        raise

def lock_pdf_with_password(in_pdf: str, out_pdf: str, password: str):
    """Lock PDF with password"""
    try:
        with pikepdf.open(in_pdf) as pdf:
            enc = pikepdf.Encryption(user=password, owner=password, R=4)
            pdf.save(out_pdf, encryption=enc)
    except Exception as e:
        logger.error(f"Error locking PDF: {e}")
        raise

def unlock_pdf(in_pdf: str, out_pdf: str, password: str):
    """Unlock PDF with password"""
    try:
        with pikepdf.open(in_pdf, password=password) as pdf:
            pdf.save(out_pdf)
    except Exception as e:
        logger.error(f"Error unlocking PDF: {e}")
        raise

def remove_pages_by_numbers(in_pdf: str, out_pdf: str, pages: List[int]):
    """Remove specified pages from PDF"""
    try:
        with pikepdf.open(in_pdf) as pdf:
            if not pages:
                pdf.save(out_pdf)
                return
            
            n = len(pdf.pages)
            for p in sorted(set(pages), reverse=True):
                if 1 <= p <= n:
                    del pdf.pages[p - 1]
            
            if len(pdf.pages) == 0:
                raise ValueError("All pages were removed")
            
            pdf.save(out_pdf)
    except Exception as e:
        logger.error(f"Error removing pages: {e}")
        raise

def extract_page_to_png(pdf_path: str, page_number: int, out_png: str, zoom: float = 2.0) -> str:
    """Extract a page from PDF as PNG image"""
    try:
        with fitz.open(pdf_path) as doc:
            if page_number < 1 or page_number > len(doc):
                raise ValueError(f"Page {page_number} out of bounds")
            
            page = doc[page_number - 1]
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            pix.save(out_png)
        
        return out_png
    except Exception as e:
        logger.error(f"Error extracting page: {e}")
        raise

# Process individual PDF operations

async def process_unlock(client: Client, message: Message, user_id: int, password: str):
    """Process unlock operation"""
    session = ensure_session_dict(user_id)
    file_id = session.get('file_id')
    file_name = session.get('file_name', 'document.pdf')
    
    if not file_id:
        await message.reply_text("❌ No file in session")
        return
    
    set_processing_flag(user_id, message.chat.id, "unlock")
    
    try:
        user_dir = get_user_temp_dir(user_id)
        in_path = await client.download_media(file_id, file_name=user_dir / 'unlock_input.pdf')
        out_path = str(user_dir / f"unlocked_{file_name}")
        
        # Unlock PDF
        unlock_pdf(in_path, out_path, password)
        
        # Send unlocked file
        cleaned_name = build_final_filename(user_id, file_name)
        delay = session.get('delete_delay', 300)
        
        await send_and_delete(client, message.chat.id, out_path, cleaned_name, delay_seconds=delay)
        await message.reply_text(MESSAGES['success_unlock'])
        
    except pikepdf.PasswordError:
        await message.reply_text("❌ Incorrect password")
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}")
    finally:
        clear_processing_flag(user_id, "unlock", "completed")

async def process_pages(client: Client, message: Message, user_id: int, pages_spec: str):
    """Process page removal operation"""
    session = ensure_session_dict(user_id)
    file_id = session.get('file_id')
    file_name = session.get('file_name', 'document.pdf')
    
    if not file_id:
        await message.reply_text("❌ No file in session")
        return
    
    set_processing_flag(user_id, message.chat.id, "pages")
    
    try:
        # Parse pages
        pages, error = parse_pages_text(pages_spec)
        if error:
            await message.reply_text(f"❌ {error}")
            return
        
        user_dir = get_user_temp_dir(user_id)
        in_path = await client.download_media(file_id, file_name=user_dir / 'pages_input.pdf')
        out_path = str(user_dir / f"pages_{file_name}")
        
        # Remove pages
        remove_pages_by_numbers(in_path, out_path, pages)
        
        # Send processed file
        cleaned_name = build_final_filename(user_id, file_name)
        delay = session.get('delete_delay', 300)
        
        await send_and_delete(client, message.chat.id, out_path, cleaned_name, delay_seconds=delay)
        await message.reply_text(MESSAGES['success_pages'])
        
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}")
    finally:
        clear_processing_flag(user_id, "pages", "completed")

async def process_add_banner(client: Client, message: Message, user_id: int):
    """Process add banner operation"""
    session = ensure_session_dict(user_id)
    file_id = session.get('file_id')
    file_name = session.get('file_name', 'document.pdf')
    
    if not file_id:
        await message.reply_text("❌ No file in session")
        return
    
    banner_pdf = await _ensure_banner_pdf_path(user_id)
    if not banner_pdf:
        await message.reply_text("❌ No default banner. Use /setbanner first.")
        return
    
    set_processing_flag(user_id, message.chat.id, "add_banner")
    status = await message.reply_text(MESSAGES['processing'])
    
    try:
        user_dir = get_user_temp_dir(user_id)
        in_path = await client.download_media(file_id, file_name=user_dir / 'banner_input.pdf')
        out_path = str(user_dir / file_name)
        
        # Add banner
        await add_banner_pages_to_pdf(in_path, out_path, banner_pdf, place='after')
        
        # Send file with banner
        cleaned_name = build_final_filename(user_id, file_name)
        delay = session.get('delete_delay', 300)
        
        await send_and_delete(client, message.chat.id, out_path, cleaned_name, delay_seconds=delay)
        await status.delete()
        await message.reply_text("✅ Banner added successfully!")
        
    except Exception as e:
        await status.edit_text(f"❌ Error: {e}")
    finally:
        clear_processing_flag(user_id, "add_banner", "completed")

async def process_lock(client: Client, message: Message, user_id: int):
    """Process lock operation"""
    session = ensure_session_dict(user_id)
    file_id = session.get('file_id')
    file_name = session.get('file_name', 'document.pdf')
    
    if not file_id:
        await message.reply_text("❌ No file in session")
        return
    
    # Get password from settings
    settings = await db.get_user_settings(user_id)
    password = settings.get('lock_password')
    
    if not password:
        await message.reply_text("ℹ️ No default password set. Use /setpassword first.")
        return
    
    set_processing_flag(user_id, message.chat.id, "lock")
    status = await message.reply_text(MESSAGES['processing'])
    
    try:
        user_dir = get_user_temp_dir(user_id)
        in_path = await client.download_media(file_id, file_name=user_dir / 'lock_input.pdf')
        out_path = str(user_dir / f"locked_{file_name}")
        
        # Lock PDF
        lock_pdf_with_password(in_path, out_path, password)
        
        # Send locked file
        cleaned_name = build_final_filename(user_id, file_name)
        delay = session.get('delete_delay', 300)
        
        await send_and_delete(client, message.chat.id, out_path, cleaned_name, delay_seconds=delay)
        await status.delete()
        await message.reply_text("✅ PDF locked successfully!")
        
    except Exception as e:
        await status.edit_text(f"❌ Error: {e}")
    finally:
        clear_processing_flag(user_id, "lock", "completed")

# Admin commands

@Client.on_message(filters.command("setbanner") & filters.private)
async def cmd_setbanner(client: Client, message: Message):
    """Set default banner"""
    user_id = message.from_user.id
    
    if not await is_user_in_channel(client, user_id):
        await send_force_join_message(client, message)
        return
    
    session = ensure_session_dict(user_id)
    session['awaiting_banner_upload'] = True
    
    await message.reply_text("🖼️ Send me your banner (image or 1-page PDF).")

@Client.on_message(filters.command("setpassword") & filters.private)
async def cmd_setpassword(client: Client, message: Message):
    """Set default password"""
    user_id = message.from_user.id
    
    if not await is_user_in_channel(client, user_id):
        await send_force_join_message(client, message)
        return
    
    args = message.text.split(maxsplit=1)
    
    if len(args) < 2:
        session = ensure_session_dict(user_id)
        session['awaiting_password'] = True
        await message.reply_text("🔐 Send your default password (or 'none' to disable).")
        return
    
    password = args[1].strip()
    
    if password.lower() in ("none", "off", "disable"):
        await db.update_user_settings(user_id, lock_password=None)
        await message.reply_text("🔓 Default password removed.")
    else:
        await db.update_user_settings(user_id, lock_password=password)
        await message.reply_text("🔐 Default password saved.")

@Client.on_message(filters.command("status") & filters.private)
async def status_handler(client: Client, message: Message):
    """Show bot status"""
    user_id = message.from_user.id
    
    if not await is_user_in_channel(client, user_id):
        await send_force_join_message(client, message)
        return
    
    # Get statistics
    user_count = await db.count_users()
    stats = await db.get_stats()
    
    from utils.helpers import format_bytes, format_uptime
    import time
    
    uptime = format_uptime(time.time() - time.time())
    
    text = (
        "📊 **Bot Status**\n\n"
        f"👥 Total Users: {user_count:,}\n"
        f"📄 Files Processed: {stats['files']:,}\n"
        f"💾 Storage Used: {format_bytes(stats['storage_bytes'])}\n"
    )
    
    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
