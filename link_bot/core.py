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
import time

import pikepdf
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.enums import ParseMode
from PIL import Image
import fitz  # PyMuPDF
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.colors import black, white, HexColor

from utils.database import db
from utils.sessions import sessions, ensure_session_dict, set_processing_flag, clear_processing_flag, reset_user_state
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
from link_bot.batch_state import user_batches, MAX_BATCH_FILES
from utils.banner_cleaner import clean_pdf_banners

logger = logging.getLogger(__name__)

# Banner storage
BANNERS_DIR = Path("banners")
BANNERS_DIR.mkdir(exist_ok=True)

# Uptime reference
START_TS = time.time()

# Debug echo (optional via env DEBUG_ECHO=1)
import os as _os
_DEBUG_ECHO = str(_os.getenv('DEBUG_ECHO', '0')).strip() not in {'0', 'false', 'False', ''}

@Client.on_message()
async def debug_echo(client: Client, message: Message):
    if not _DEBUG_ECHO:
        return
    try:
        logger.info(f"[DEBUG ECHO] Got message from {getattr(message.from_user, 'id', None)}: {getattr(message, 'text', None)!r}")
        await message.reply_text(f"✅ Received (debug echo): {getattr(message, 'text', '')}")
    except Exception as e:
        logger.error(f"debug_echo error: {e}")

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
    
    # Track & log user/message
    await db.track_user(user_id)
    try:
        await db.save_message(user_id, message)
    except Exception:
        pass
    
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
    
    # Track & log user/message
    await db.track_user(user_id)
    try:
        await db.save_message(user_id, message)
        if message.document:
            await db.save_file(user_id, message.document)
    except Exception:
        pass
    
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

def is_pdf_locked(pdf_path: str) -> bool:
    """Check if a PDF is password protected."""
    try:
        with pikepdf.open(pdf_path):
            return False
    except pikepdf.PasswordError:
        return True
    except Exception:
        return False

def create_default_banner_pdf(user_id: int, username: Optional[str] = None) -> Optional[str]:
    """Create a simple default 1-page banner PDF and return its path."""
    try:
        tmp_dir = get_user_temp_dir(user_id)
        out_path = tmp_dir / f"default_banner_{user_id}.pdf"

        c = canvas.Canvas(str(out_path), pagesize=letter)
        width, height = letter

        primary = HexColor('#2E86AB')
        secondary = HexColor('#A23B72')
        bg = HexColor('#F8F9FA')

        # Background
        c.setFillColor(bg)
        c.rect(0, 0, width, height, fill=1, stroke=0)

        # Top band
        c.setFillColor(primary)
        c.rect(0, height - 80, width, 80, fill=1, stroke=0)

        # Bottom band
        c.setFillColor(secondary)
        c.rect(0, 0, width, 40, fill=1, stroke=0)

        # Title
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 26)
        c.drawCentredString(width / 2, height - 45, "DOCUMENT PROCESSED")

        # Username
        c.setFont("Helvetica", 14)
        who = username or ensure_session_dict(user_id).get('username') or f"User {user_id}"
        c.drawCentredString(width / 2, height - 70, f"by {who}")

        # Center text
        c.setFillColor(primary)
        c.setFont("Helvetica-Bold", 18)
        c.drawCentredString(width / 2, height / 2 + 20, "✓ VERIFIED")
        c.setFillColor(black)
        c.setFont("Helvetica", 12)
        c.drawCentredString(width / 2, height / 2 - 5, "This document has been processed")

        c.showPage()
        c.save()

        return str(out_path)
    except Exception as e:
        logger.error(f"create_default_banner_pdf error: {e}")
        return None

async def process_full_pipeline(client: Client, chat_id: int, user_id: int, file_id: str, file_name: str,
                                unlock_pw: Optional[str], pages_to_remove: Optional[List[int]], lock_pw: Optional[str]):
    """Run Unlock -> Clean banners -> Add banner -> Remove pages -> Lock pipeline for a single file."""
    session = ensure_session_dict(user_id)
    user_dir = get_user_temp_dir(user_id)

    status = await client.send_message(chat_id, "⏳ Full Process in progress...")
    try:
        in_path = await client.download_media(file_id, file_name=user_dir / 'fullproc_input.pdf')
        current = in_path

        # 1) Unlock
        if unlock_pw and unlock_pw.lower() != 'none':
            tmp = str(user_dir / 'fullproc_unlocked.pdf')
            with pikepdf.open(current, password=unlock_pw) as pdf:
                pdf.save(tmp)
            current = tmp

        # 2) Clean banners
        with open(current, 'rb') as f:
            pdf_bytes = f.read()
        cleaned = clean_pdf_banners(pdf_bytes, user_id)
        tmp2 = str(user_dir / 'fullproc_cleaned.pdf')
        with open(tmp2, 'wb') as f:
            f.write(cleaned)
        current = tmp2

        # 3) Add banner (ensure exists or create default)
        banner_pdf = await _ensure_banner_pdf_path(user_id)
        if not banner_pdf:
            banner_pdf = create_default_banner_pdf(user_id)
        if banner_pdf:
            tmp3 = str(user_dir / 'fullproc_bannered.pdf')
            await add_banner_pages_to_pdf(current, tmp3, banner_pdf, place='after')
            current = tmp3

        # 4) Remove pages
        if pages_to_remove:
            tmp4 = str(user_dir / 'fullproc_paged.pdf')
            remove_pages_by_numbers(current, tmp4, pages_to_remove)
            current = tmp4

        # 5) Lock
        if lock_pw:
            tmp5 = str(user_dir / 'fullproc_locked.pdf')
            lock_pdf_with_password(current, tmp5, lock_pw)
            current = tmp5

        # Send result
        final_name = build_final_filename(user_id, file_name)
        delay = session.get('delete_delay', 300)
        await send_and_delete(client, chat_id, current, final_name, delay_seconds=delay)
        await status.edit_text("✅ Full Process done!")
    except pikepdf.PasswordError:
        await status.edit_text("❌ Incorrect password for unlocking.")
    except Exception as e:
        await status.edit_text(f"❌ Error: {e}")

async def process_extract_page(client: Client, message: Message, user_id: int, page_number: int):
    """Download current PDF and send extracted page as image."""
    session = ensure_session_dict(user_id)
    file_id = session.get('file_id')
    file_name = session.get('file_name', 'document.pdf')
    if not file_id:
        await message.reply_text("❌ No PDF in session. Send a PDF first.")
        return

    user_dir = get_user_temp_dir(user_id)
    status = await message.reply_text("⏳ Extracting page...")
    try:
        pdf_path = await client.download_media(file_id, file_name=user_dir / 'extract_input.pdf')
        if is_pdf_locked(pdf_path):
            await status.edit_text("❌ PDF is locked. Unlock it first.")
            return
        out_path = str(user_dir / f"{Path(file_name).stem}_page_{page_number}.png")
        extract_page_to_png(pdf_path, page_number, out_path, zoom=3.0)
        await client.send_photo(message.chat.id, out_path, caption=f"📌 Page {page_number} of {file_name}")
        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ Error: {e}")
    finally:
        # best-effort cleanup
        try:
            for p in (user_dir / 'extract_input.pdf',):
                if isinstance(p, Path):
                    if p.exists():
                        p.unlink()
        except Exception:
            pass

def _settings_keyboard(user_id: int, current_pos: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📍 At start {'✓' if current_pos=='start' else ''}", callback_data=f"set_position_start:{user_id}")],
        [InlineKeyboardButton(f"📍 At end {'✓' if current_pos=='end' else ''}", callback_data=f"set_position_end:{user_id}")],
        [InlineKeyboardButton("🕒 Set auto-delete delay", callback_data=f"set_delay:{user_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")],
    ])

def _pages_quick_keyboard(user_id: int, mode: str = 'pages') -> InlineKeyboardMarkup:
    """Quick selection keyboard for pages/both/fullproc.
    mode in {'pages','both','full'} determines callback prefix.
    """
    prefix = {
        'pages': 'the',
        'both': 'both',
        'full': 'full',
    }.get(mode, 'the')
    rows = [
        [
            InlineKeyboardButton("The First", callback_data=f"{prefix}_first:{user_id}"),
            InlineKeyboardButton("The Last", callback_data=f"{prefix}_last:{user_id}"),
        ],
        [InlineKeyboardButton("The Middle", callback_data=f"{prefix}_middle:{user_id}")],
    ]
    if mode == 'full':
        rows.append([InlineKeyboardButton("None", callback_data=f"{prefix}_none:{user_id}")])
    rows.append([InlineKeyboardButton("📝 Enter manually", callback_data=f"{ 'enter_manually' if mode=='pages' else prefix + '_manual'}:{user_id}")])
    return InlineKeyboardMarkup(rows)


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
    
    uptime = format_uptime(time.time() - START_TS)
    
    text = (
        "📊 **Bot Status**\n\n"
        f"👥 Total Users: {user_count:,}\n"
        f"📄 Files Processed: {stats['files']:,}\n"
        f"💾 Storage Used: {format_bytes(stats['storage_bytes'])}\n"
    )
    
    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

@Client.on_message(filters.command("ping") & filters.private)
async def ping_handler(client: Client, message: Message):
    """Quick health check."""
    await message.reply_text("pong")

@Client.on_message(filters.command("debug") & filters.private)
async def debug_handler(client: Client, message: Message):
    """Show brief session info for troubleshooting."""
    user_id = message.from_user.id
    session = ensure_session_dict(user_id)
    keys = [k for k in session.keys() if not str(k).startswith('_')]
    info = "\n".join(f"- {k}: {session.get(k)}" for k in keys) or "(empty)"
    await message.reply_text(f"Session keys:\n{info}")

@Client.on_message(filters.command("debug_on") & filters.private)
async def debug_on_handler(client: Client, message: Message):
    """Enable message tap logging for 5 minutes."""
    user_id = message.from_user.id
    session = ensure_session_dict(user_id)
    session['debug_log'] = True
    await message.reply_text("✅ Debug logging enabled for 5 minutes.")
    async def _disable():
        await asyncio.sleep(300)
        session['debug_log'] = False
    asyncio.create_task(_disable())

@Client.on_message(filters.private)
async def _debug_tap(client: Client, message: Message):
    try:
        user_id = message.from_user.id
    except Exception:
        return
    session = ensure_session_dict(user_id)
    if session.get('debug_log'):
        try:
            logger.info(f"[DEBUG_TAP] msg_id={message.id} type={type(message) } text={getattr(message, 'text', '')!r}")
        except Exception:
            pass

# ===== Settings and callbacks =====

@Client.on_callback_query(filters.regex(r"^settings$"))
async def settings_menu(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    session = ensure_session_dict(user_id)
    pos = session.get('text_position', 'end')
    kb = _settings_keyboard(user_id, pos)
    await query.edit_message_text(
        f"⚙️ **Settings**\n\nText position: **{pos}**\nAuto-delete delay: {session.get('delete_delay', 300)}s",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )

@Client.on_callback_query(filters.regex(r"^set_position_start:(\d+)$"))
async def set_position_start_cb(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    await db.update_user_settings(user_id, text_position='start')
    session = ensure_session_dict(user_id)
    session['text_position'] = 'start'
    await settings_menu(client, query)

@Client.on_callback_query(filters.regex(r"^set_position_end:(\d+)$"))
async def set_position_end_cb(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    await db.update_user_settings(user_id, text_position='end')
    session = ensure_session_dict(user_id)
    session['text_position'] = 'end'
    await settings_menu(client, query)

@Client.on_callback_query(filters.regex(r"^set_delay:(\d+)$"))
async def set_delay_cb(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    session = ensure_session_dict(user_id)
    session['awaiting_delete_delay'] = True
    await query.edit_message_text("🕒 Send auto-delete delay in seconds (e.g. 300).")

@Client.on_callback_query(filters.regex(r"^back_main$"))
async def back_main_cb(client: Client, query: CallbackQuery):
    await send_welcome_message(client, query.from_user.id)

# ===== PDF actions callbacks =====

@Client.on_callback_query(filters.regex(r"^(rename_file|unlock|pages|both|fullproc|add_banner|lock_now|cancel):(\d+)$"))
async def pdf_actions_cb(client: Client, query: CallbackQuery):
    data = query.data
    action, uid = data.split(":", 1)
    user_id = int(uid)

    # Verify user
    if query.from_user.id != user_id:
        await query.answer("❌ This is not for you!", show_alert=True)
        return

    session = ensure_session_dict(user_id)

    if action == 'rename_file':
        await query.edit_message_text("📝 Send the new base name (without extension) or send `auto` to auto-clean.")
        session['awaiting_rename'] = True
        return

    if action == 'unlock':
        session['awaiting_unlock_password'] = True
        await query.edit_message_text("🔐 Send the password to unlock this PDF:")
        return

    if action == 'pages':
        session['awaiting_pages'] = True
        await query.edit_message_text(
            "📝 Remove Pages — choose a quick option or enter manually.",
            reply_markup=_pages_quick_keyboard(user_id, 'pages'),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if action == 'both':
        session['awaiting_both_password'] = True
        await query.edit_message_text("🛠️ The Both: Step 1/2 — send unlock password (or `none`).")
        return

    if action == 'fullproc':
        session['awaiting_fullproc_password'] = True
        await query.edit_message_text("⚡ Full Process: Step 1/3 — send unlock password (or `none`).")
        return

    if action == 'add_banner':
        await process_add_banner(client, query.message, user_id)
        return

    if action == 'lock_now':
        await process_lock(client, query.message, user_id)
        return

    if action == 'cancel':
        reset_user_state(user_id)
        await query.edit_message_text("✅ Cancelled.")
        return

# ===== Text handlers for interactive flows =====

@Client.on_message(filters.text & filters.private)
async def handle_text_flows(client: Client, message: Message):
    user_id = message.from_user.id
    try:
        await db.save_message(user_id, message)
    except Exception:
        pass
    session = ensure_session_dict(user_id)

    # Fallback: treat '/start' typed as plain text like the /start command
    txt = (message.text or '').strip().lower()
    if txt.startswith('/start'):
        await start_handler(client, message)
        return

    # Settings: delete delay
    if session.get('awaiting_delete_delay'):
        session.pop('awaiting_delete_delay', None)
        txt = (message.text or '').strip()
        try:
            value = max(0, int(txt))
        except Exception:
            await message.reply_text("❌ Invalid number. Try again from Settings.")
            return
        session['delete_delay'] = value
        await db.update_user_settings(user_id, delete_delay=value)
        await message.reply_text(f"✅ Auto-delete delay set to {value}s.")
        return

    # Rename
    if session.get('awaiting_rename'):
        session.pop('awaiting_rename', None)
        base = (message.text or '').strip()
        try:
            await message.delete()
        except Exception:
            pass
        original = session.get('file_name', 'document.pdf')
        if base.lower() == 'auto' or not base:
            new_name = build_final_filename(user_id, original)
        else:
            root, _ = os.path.splitext(original)
            # Keep extension from original
            ext = os.path.splitext(original)[1] or '.pdf'
            safe = ''.join(c for c in base if c not in '\\/:*?"<>|').strip() or root
            pos = session.get('text_position', 'end')
            tag = session.get('username')
            final_base = f"{tag} {safe}" if (tag and pos == 'start') else (f"{safe} {tag}" if tag else safe)
            new_name = f"{final_base}{ext}"
        # Send original file with new name
        file_id = session.get('file_id')
        if not file_id:
            await message.reply_text("❌ No file in session")
            return
        user_dir = get_user_temp_dir(user_id)
        in_path = await client.download_media(file_id, file_name=user_dir / 'rename_input.pdf')
        delay = session.get('delete_delay', 300)
        await send_and_delete(client, message.chat.id, in_path, new_name, delay_seconds=delay)
        await message.reply_text("✅ File renamed and sent.")
        return

    # Unlock password
    if session.get('awaiting_unlock_password'):
        session.pop('awaiting_unlock_password', None)
        password = (message.text or '').strip()
        try:
            await message.delete()
        except Exception:
            pass
        await process_unlock(client, message, user_id, password)
        return

    # Pages removal
    if session.get('awaiting_pages'):
        session.pop('awaiting_pages', None)
        pages_spec = (message.text or '').strip()
        try:
            await message.delete()
        except Exception:
            pass
        await process_pages(client, message, user_id, pages_spec)
        return

    # The Both flow
    if session.get('awaiting_both_password'):
        session.pop('awaiting_both_password', None)
        session['both_password'] = (message.text or '').strip()
        try:
            await message.delete()
        except Exception:
            pass
        await message.reply_text("🛠️ The Both: Step 2/2 — send pages to remove (e.g. `1,3-5`) or `none`.", parse_mode=ParseMode.MARKDOWN)
        session['awaiting_both_pages'] = True
        return
    if session.get('awaiting_both_pages'):
        session.pop('awaiting_both_pages', None)
        pages_spec = (message.text or '').strip()
        password = session.pop('both_password', '')
        try:
            await message.delete()
        except Exception:
            pass
        # Run unlock then pages sequentially
        if password and password.lower() != 'none':
            try:
                await process_unlock(client, message, user_id, password)
            except Exception:
                pass
        await process_pages(client, message, user_id, pages_spec)
        return

    # Full Process flow
    if session.get('awaiting_fullproc_password'):
        session.pop('awaiting_fullproc_password', None)
        session['fullproc_password'] = (message.text or '').strip()
        try:
            await message.delete()
        except Exception:
            pass
        await message.reply_text(
            "⚡ Full Process: Step 2/3 — choose quick pages or enter manually.",
            reply_markup=_pages_quick_keyboard(user_id, 'full'),
            parse_mode=ParseMode.MARKDOWN,
        )
        session['awaiting_fullproc_pages'] = True
        return
    if session.get('awaiting_fullproc_pages'):
        session.pop('awaiting_fullproc_pages', None)
        session['fullproc_pages'] = (message.text or '').strip()
        try:
            await message.delete()
        except Exception:
            pass
        await message.reply_text("⚡ Full Process: Step 3/3 — lock password (or `skip`).", parse_mode=ParseMode.MARKDOWN)
        session['awaiting_fullproc_lock'] = True
        return
    if session.get('awaiting_fullproc_lock'):
        session.pop('awaiting_fullproc_lock', None)
        lock_pw = (message.text or '').strip()
        try:
            await message.delete()
        except Exception:
            pass
        unlock_pw = session.pop('fullproc_password', '')
        pages_list = session.pop('fullproc_pages_list', None)
        if pages_list is None:
            pages_text = session.pop('fullproc_pages', 'none')
            from utils.helpers import parse_pages_spec
            pages_to_remove = [] if pages_text.strip().lower() in {'none', 'no', 'skip', '0'} else list(parse_pages_spec(pages_text))
        else:
            pages_to_remove = list(pages_list)
        if lock_pw.lower() in {'skip', 'none', 'no'}:
            lock_pw = ''
        file_id = session.get('file_id')
        file_name = session.get('file_name', 'document.pdf')
        if not file_id:
            await message.reply_text("❌ No file in session")
            return
        await process_full_pipeline(client, message.chat.id, user_id, file_id, file_name, unlock_pw, pages_to_remove, lock_pw)
        return

# ===== Extract page feature =====

@Client.on_message(filters.command("setextra_pages") & filters.private)
async def cmd_setextra_pages(client: Client, message: Message):
    user_id = message.from_user.id
    session = ensure_session_dict(user_id)
    session['awaiting_extract_page'] = True
    await message.reply_text("📄 Send the page number to extract as PNG (e.g. 1)")

@Client.on_message(filters.text & filters.private)
async def handle_extract_page_step(client: Client, message: Message):
    user_id = message.from_user.id
    session = ensure_session_dict(user_id)
    if not session.get('awaiting_extract_page'):
        return
    session.pop('awaiting_extract_page', None)
    txt = (message.text or '').strip()
    try:
        page_num = int(txt)
        if page_num < 1:
            raise ValueError
    except Exception:
        await message.reply_text("❌ Invalid page number.")
        return
    await process_extract_page(client, message, user_id, page_num)

# ===== Quick page selection callbacks (single file) =====

@Client.on_callback_query(filters.regex(r"^the_first:(\d+)$"))
async def cb_the_first(client: Client, query: CallbackQuery):
    user_id = int(query.matches[0].group(1))
    if query.from_user.id != user_id:
        await query.answer("❌ This is not for you!", show_alert=True)
        return
    await query.answer()
    await process_pages(client, query.message, user_id, "1")

@Client.on_callback_query(filters.regex(r"^the_last:(\d+)$"))
async def cb_the_last(client: Client, query: CallbackQuery):
    user_id = int(query.matches[0].group(1))
    if query.from_user.id != user_id:
        await query.answer("❌ This is not for you!", show_alert=True)
        return
    await query.answer()
    # Compute last by opening the current file quickly
    session = ensure_session_dict(user_id)
    file_id = session.get('file_id')
    if not file_id:
        await query.edit_message_text("❌ No PDF in session")
        return
    user_dir = get_user_temp_dir(user_id)
    path = await client.download_media(file_id, file_name=user_dir / 'quick_last.pdf')
    try:
        with pikepdf.open(path) as pdf:
            last = len(pdf.pages)
        await process_pages(client, query.message, user_id, str(last))
    except Exception as e:
        await query.edit_message_text(f"❌ Error: {e}")

@Client.on_callback_query(filters.regex(r"^the_middle:(\d+)$"))
async def cb_the_middle(client: Client, query: CallbackQuery):
    user_id = int(query.matches[0].group(1))
    if query.from_user.id != user_id:
        await query.answer("❌ This is not for you!", show_alert=True)
        return
    await query.answer()
    session = ensure_session_dict(user_id)
    file_id = session.get('file_id')
    if not file_id:
        await query.edit_message_text("❌ No PDF in session")
        return
    user_dir = get_user_temp_dir(user_id)
    path = await client.download_media(file_id, file_name=user_dir / 'quick_middle.pdf')
    try:
        with pikepdf.open(path) as pdf:
            total = len(pdf.pages)
            middle = max(1, total // 2)
        await process_pages(client, query.message, user_id, str(middle))
    except Exception as e:
        await query.edit_message_text(f"❌ Error: {e}")

@Client.on_callback_query(filters.regex(r"^enter_manually:(\d+)$"))
async def cb_enter_manually(client: Client, query: CallbackQuery):
    user_id = int(query.matches[0].group(1))
    if query.from_user.id != user_id:
        await query.answer("❌ This is not for you!", show_alert=True)
        return
    await query.answer()
    session = ensure_session_dict(user_id)
    session['awaiting_pages'] = True
    await query.edit_message_text("📝 Send pages to remove (e.g. `1,3-5`) or `none`.", parse_mode=ParseMode.MARKDOWN)

# The Both quick pages
@Client.on_callback_query(filters.regex(r"^both_(first|last|middle|manual):(\d+)$"))
async def cb_both_quick(client: Client, query: CallbackQuery):
    action, uid = query.data.split(":", 1)
    user_id = int(uid)
    if query.from_user.id != user_id:
        await query.answer("❌ This is not for you!", show_alert=True)
        return
    await query.answer()
    kind = action.split("_", 1)[1]
    session = ensure_session_dict(user_id)
    if kind == 'manual':
        session['awaiting_both_pages'] = True
        await query.edit_message_text("📝 Send pages to remove (e.g. `1,3-5`) or `none`.", parse_mode=ParseMode.MARKDOWN)
        return
    # need password first if not set
    if 'both_password' not in session:
        session['awaiting_both_password'] = True
        await query.edit_message_text("🛠️ The Both: Step 1/2 — send unlock password (or `none`).")
        return
    # compute pages
    if kind == 'first':
        pages = '1'
    elif kind == 'last':
        # Determine last dynamically
        file_id = session.get('file_id')
        if not file_id:
            await query.edit_message_text("❌ No PDF in session")
            return
        path = await client.download_media(file_id, file_name=get_user_temp_dir(user_id) / 'both_last.pdf')
        with pikepdf.open(path) as pdf:
            pages = str(len(pdf.pages))
    else:
        file_id = session.get('file_id')
        if not file_id:
            await query.edit_message_text("❌ No PDF in session")
            return
        path = await client.download_media(file_id, file_name=get_user_temp_dir(user_id) / 'both_middle.pdf')
        with pikepdf.open(path) as pdf:
            pages = str(max(1, len(pdf.pages) // 2))
    password = session.get('both_password', '')
    await process_unlock(client, query.message, user_id, password)
    await process_pages(client, query.message, user_id, pages)

# Full Process quick pages
@Client.on_callback_query(filters.regex(r"^full_(first|last|middle|none|manual):(\d+)$"))
async def cb_full_quick(client: Client, query: CallbackQuery):
    action, uid = query.data.split(":", 1)
    user_id = int(uid)
    if query.from_user.id != user_id:
        await query.answer("❌ This is not for you!", show_alert=True)
        return
    await query.answer()
    kind = action.split("_", 1)[1]
    session = ensure_session_dict(user_id)
    if 'fullproc_password' not in session and kind != 'manual':
        session['awaiting_fullproc_password'] = True
        await query.edit_message_text("⚡ Full Process: Step 1/3 — send unlock password (or `none`).")
        return
    if kind == 'manual':
        session['awaiting_fullproc_pages'] = True
        await query.edit_message_text(
            "⚡ Full Process: Step 2/3 — enter pages to remove (e.g. `1,3-5`) or `none`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    # Determine pages
    pages_to_remove: List[int] = []
    if kind == 'none':
        pages_to_remove = []
    else:
        file_id = session.get('file_id')
        if not file_id:
            await query.edit_message_text("❌ No PDF in session")
            return
        path = await client.download_media(file_id, file_name=get_user_temp_dir(user_id) / f'full_{kind}.pdf')
        with pikepdf.open(path) as pdf:
            if kind == 'first':
                pages_to_remove = [1]
            elif kind == 'last':
                pages_to_remove = [len(pdf.pages)]
            else:
                pages_to_remove = [max(1, len(pdf.pages) // 2)]
    unlock_pw = session.get('fullproc_password', '')
    # Ask for lock password and then execute
    session['fullproc_pages_list'] = pages_to_remove
    session['awaiting_fullproc_lock'] = True
    await query.edit_message_text("⚡ Full Process: Step 3/3 — lock password (or `skip`).", parse_mode=ParseMode.MARKDOWN)
