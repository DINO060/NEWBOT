"""
Batch/Sequence mode processing for PDF Bot
"""
import os
import tempfile
import shutil
import logging
import asyncio
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime

import pikepdf
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.enums import ParseMode

from utils.database import db
from utils.sessions import sessions, ensure_session_dict
from utils.helpers import (
    build_final_filename, 
    clean_caption_with_username,
    get_user_temp_dir,
    send_and_delete,
    create_or_edit_status,
    is_pdf_file
)
from utils.banner_cleaner import clean_pdf_banners
from link_bot.core import (
    _ensure_banner_pdf_path,
    add_banner_pages_to_pdf,
    lock_pdf_with_password,
    remove_pages_by_numbers,
)

logger = logging.getLogger(__name__)

from link_bot.batch_state import user_batches, MAX_BATCH_FILES

def clear_user_batch(user_id: int) -> None:
    """Clear user's batch"""
    user_batches[user_id] = []
    # Also clear from database
    asyncio.create_task(db.clear_batch(user_id))

def get_batch_pages_buttons(user_id: int) -> InlineKeyboardMarkup:
    """Build batch pages selection keyboard"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("The First", callback_data=f"batch_pages_first:{user_id}"),
            InlineKeyboardButton("The Last", callback_data=f"batch_pages_last:{user_id}"),
        ],
        [
            InlineKeyboardButton("The Middle", callback_data=f"batch_pages_middle:{user_id}"),
        ],
        [
            InlineKeyboardButton("📝 Enter manually", callback_data=f"batch_pages_manual:{user_id}"),
        ],
    ])

def get_batch_both_buttons(user_id: int) -> InlineKeyboardMarkup:
    """Build batch 'The Both' pages selection keyboard"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("The First", callback_data=f"batch_both_first:{user_id}"),
            InlineKeyboardButton("The Last", callback_data=f"batch_both_last:{user_id}"),
        ],
        [
            InlineKeyboardButton("The Middle", callback_data=f"batch_both_middle:{user_id}"),
        ],
        [
            InlineKeyboardButton("📝 Enter manually", callback_data=f"batch_both_manual:{user_id}"),
        ],
    ])

@Client.on_message(filters.command("batch") & filters.private)
async def batch_command(client: Client, message: Message):
    """Enable batch/sequence mode"""
    user_id = message.from_user.id
    
    # Track user
    await db.track_user(user_id)
    
    # Force-join check
    from link_bot.admin import is_user_in_channel, send_force_join_message
    if not await is_user_in_channel(client, user_id):
        await send_force_join_message(client, message)
        return
    
    # Ensure session exists
    session = ensure_session_dict(user_id)
    session['last_activity'] = datetime.now()
    
    # Enable batch mode
    session['batch_mode'] = True
    
    # Initialize batch if not exists
    if user_id not in user_batches:
        user_batches[user_id] = []
    
    # Load batch from database if empty in memory
    if not user_batches[user_id]:
        user_batches[user_id] = await db.get_batch_files(user_id)
    
    count = len(user_batches[user_id])
    
    if count > 0:
        msg = (
            f"📦 **Sequence Mode**\n\n"
            f"✅ You have {count} file(s) waiting\n"
            f"📊 Maximum: {MAX_BATCH_FILES} files\n\n"
            f"🔄 Send `/process` to process all files"
        )
    else:
        msg = (
            f"📦 **Sequence Mode**\n\n"
            f"📭 No files waiting\n\n"
            f"✅ You can send up to {MAX_BATCH_FILES} files\n"
            f"📄 PDFs will be processed when you send `/process`\n\n"
            f"🔄 Send `/process` when you're done adding files"
        )
    
    await message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

@Client.on_message(filters.command("process") & filters.private)
async def process_batch_command(client: Client, message: Message):
    """Process all files in batch"""
    user_id = message.from_user.id
    
    # Force-join check
    from link_bot.admin import is_user_in_channel, send_force_join_message
    if not await is_user_in_channel(client, user_id):
        await send_force_join_message(client, message)
        return
    
    # Check batch
    if user_id not in user_batches:
        user_batches[user_id] = await db.get_batch_files(user_id)
    
    batch_files = user_batches[user_id]
    if not batch_files:
        await message.reply_text("❌ No files waiting in the batch")
        return
    
    # Filter PDF files
    pdf_files = [f for f in batch_files if is_pdf_file(f.get('file_name', ''))]
    
    if pdf_files:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔓 Unlock all", callback_data=f"batch_unlock:{user_id}")],
            [InlineKeyboardButton("🗑️ Remove pages (all)", callback_data=f"batch_pages:{user_id}")],
            [InlineKeyboardButton("🛠️ The Both (all)", callback_data=f"batch_both:{user_id}")],
            [InlineKeyboardButton("⚡ Full Process (all)", callback_data=f"batch_fullproc:{user_id}")],
            [InlineKeyboardButton("🪧 Add banner (all)", callback_data=f"batch_add_banner:{user_id}")],
            [InlineKeyboardButton("🔐 Lock all", callback_data=f"batch_lock:{user_id}")],
            [InlineKeyboardButton("🧹 Clear sequence", callback_data=f"batch_clear:{user_id}")],
        ])
        
        await message.reply_text(
            f"📦 **Sequence Processing**\n\n"
            f"{len(pdf_files)} PDF(s) ready\n\n"
            f"What do you want to do?",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await message.reply_text("✅ All files processed!")
        clear_user_batch(user_id)

async def process_batch_unlock(client: Client, message: Message, user_id: int, password: str):
    """Unlock all PDFs in batch"""
    if user_id not in user_batches:
        user_batches[user_id] = await db.get_batch_files(user_id)
    
    files = user_batches[user_id]
    pdf_files = [f for f in files if is_pdf_file(f['file_name'])]
    
    if not pdf_files:
        await client.send_message(message.chat.id, "❌ No PDF files in batch")
        return
    
    session = ensure_session_dict(user_id)
    status = await create_or_edit_status(client, message, f"⏳ Processing {len(pdf_files)} PDF files...")
    success_count = 0
    error_count = 0
    
    try:
        for i, file_info in enumerate(pdf_files):
            try:
                await status.edit_text(f"⏳ Processing file {i+1}/{len(pdf_files)}...")
                
                # Download file
                file_path = await client.download_media(
                    file_info['file_id'], 
                    file_name=f"{get_user_temp_dir(user_id)}/batch_{i}.pdf"
                )
                
                with tempfile.TemporaryDirectory() as temp_dir:
                    input_path = Path(temp_dir) / "input.pdf"
                    output_path = Path(temp_dir) / f"unlocked_{file_info['file_name']}"
                    shutil.move(file_path, input_path)
                    
                    # Unlock PDF
                    with pikepdf.open(input_path, password=password if password.lower() != 'none' else '') as pdf:
                        pdf.save(output_path)
                    
                    # Send unlocked file
                    new_file_name = build_final_filename(user_id, file_info['file_name'])
                    delay = session.get('delete_delay', 300)
                    await send_and_delete(client, message.chat.id, output_path, new_file_name, delay_seconds=delay)
                    
                    success_count += 1
                    
            except Exception as e:
                logger.error(f"Error batch unlock file {i}: {e}")
                error_count += 1
        
        await status.edit_text(
            f"✅ Processing complete!\n\n"
            f"Successful: {success_count}\n"
            f"Errors: {error_count}"
        )
        
    finally:
        clear_user_batch(user_id)
        session.pop('batch_mode', None)

async def process_batch_pages(client: Client, message: Message, user_id: int, pages_spec: str):
    """Remove pages from all PDFs in batch"""
    if user_id not in user_batches:
        user_batches[user_id] = await db.get_batch_files(user_id)
    
    files = user_batches[user_id]
    pdf_files = [f for f in files if is_pdf_file(f['file_name'])]
    
    if not pdf_files:
        await client.send_message(message.chat.id, "❌ No PDF files in batch")
        return
    
    # Parse pages specification
    from utils.helpers import parse_pages_spec
    pages_to_remove = set()
    
    spec = (pages_spec or '').strip().lower()
    if spec in {"none", "no", "skip", "0"}:
        pages_to_remove = set()
    elif spec == "first":
        pages_to_remove = {1}
    elif spec == "last":
        pages_to_remove = {"__LAST__"}
    elif spec == "middle":
        pages_to_remove = {"__MIDDLE__"}
    else:
        pages_to_remove = set(parse_pages_spec(pages_spec))
    
    session = ensure_session_dict(user_id)
    status = await create_or_edit_status(client, message, f"⏳ Processing {len(pdf_files)} PDF files...")
    success_count = 0
    error_count = 0
    
    try:
        for i, file_info in enumerate(pdf_files):
            try:
                await status.edit_text(f"⏳ Processing file {i+1}/{len(pdf_files)}...")
                
                # Download file
                file_path = await client.download_media(
                    file_info['file_id'],
                    file_name=f"{get_user_temp_dir(user_id)}/batch_{i}.pdf"
                )
                
                with tempfile.TemporaryDirectory() as temp_dir:
                    input_path = Path(temp_dir) / "input.pdf"
                    output_path = Path(temp_dir) / f"modified_{file_info['file_name']}"
                    shutil.move(file_path, input_path)
                    
                    with pikepdf.open(input_path) as pdf:
                        # Compute dynamic pages if needed
                        effective_remove = set()
                        if "__LAST__" in pages_to_remove:
                            effective_remove.add(len(pdf.pages))
                        if "__MIDDLE__" in pages_to_remove:
                            effective_remove.add(max(1, len(pdf.pages) // 2))
                        # Add static pages
                        effective_remove.update({p for p in pages_to_remove if isinstance(p, int)})
                        
                        # Keep pages not in removal list
                        pages_to_keep = [p for i, p in enumerate(pdf.pages) if (i + 1) not in effective_remove]
                        
                        if not pages_to_keep:
                            error_count += 1
                            continue
                        
                        # Create new PDF with remaining pages
                        new_pdf = pikepdf.new()
                        for page in pages_to_keep:
                            new_pdf.pages.append(page)
                        
                        new_pdf.save(output_path)
                    
                    # Send modified file
                    new_file_name = build_final_filename(user_id, file_info['file_name'])
                    delay = session.get('delete_delay', 300)
                    await send_and_delete(client, message.chat.id, output_path, new_file_name, delay_seconds=delay)
                    
                    success_count += 1
                    
            except Exception as e:
                logger.error(f"Error batch pages file {i}: {e}")
                error_count += 1
        
        await status.edit_text(
            f"✅ Processing complete!\n\n"
            f"Successful: {success_count}\n"
            f"Errors: {error_count}"
        )
        
    finally:
        clear_user_batch(user_id)
        session.pop('batch_mode', None)

async def process_batch_both(client: Client, message: Message, user_id: int, password: str, pages_spec: str):
    """Combined unlock + remove pages for all PDFs"""
    if user_id not in user_batches:
        user_batches[user_id] = await db.get_batch_files(user_id)
    
    files = user_batches[user_id]
    pdf_files = [f for f in files if is_pdf_file(f['file_name'])]
    
    if not pdf_files:
        await client.send_message(message.chat.id, "❌ No PDF files in batch")
        return
    
    # Parse pages
    from utils.helpers import parse_pages_spec
    pages_to_remove = set()
    
    spec = (pages_spec or '').strip().lower()
    if spec == "first":
        pages_to_remove = {1}
    elif spec == "last":
        pages_to_remove = {"__LAST__"}
    elif spec == "middle":
        pages_to_remove = {"__MIDDLE__"}
    else:
        pages_to_remove = set(parse_pages_spec(pages_spec))
    
    session = ensure_session_dict(user_id)
    status = await create_or_edit_status(client, message, f"⏳ Combined processing of {len(pdf_files)} PDF files...")
    success_count = 0
    error_count = 0
    
    try:
        for i, file_info in enumerate(pdf_files):
            try:
                await status.edit_text(f"⏳ Processing file {i+1}/{len(pdf_files)}...")
                
                # Download file
                file_path = await client.download_media(
                    file_info['file_id'],
                    file_name=f"{get_user_temp_dir(user_id)}/batch_{i}.pdf"
                )
                
                with tempfile.TemporaryDirectory() as temp_dir:
                    input_path = Path(temp_dir) / "input.pdf"
                    output_path = Path(temp_dir) / f"both_{file_info['file_name']}"
                    shutil.move(file_path, input_path)
                    
                    # Open with password and process
                    with pikepdf.open(input_path, password=password if password.lower() != 'none' else '') as pdf:
                        # Compute pages to remove
                        effective_remove = set()
                        if "__LAST__" in pages_to_remove:
                            effective_remove.add(len(pdf.pages))
                        if "__MIDDLE__" in pages_to_remove:
                            effective_remove.add(max(1, len(pdf.pages) // 2))
                        effective_remove.update({p for p in pages_to_remove if isinstance(p, int)})
                        
                        # Keep remaining pages
                        pages_to_keep = [p for i, p in enumerate(pdf.pages) if (i + 1) not in effective_remove]
                        
                        if not pages_to_keep:
                            error_count += 1
                            continue
                        
                        # Create new PDF
                        new_pdf = pikepdf.new()
                        for page in pages_to_keep:
                            new_pdf.pages.append(page)
                        
                        new_pdf.save(output_path)
                    
                    # Send processed file
                    new_file_name = build_final_filename(user_id, file_info['file_name'])
                    delay = session.get('delete_delay', 300)
                    await send_and_delete(client, message.chat.id, output_path, new_file_name, delay_seconds=delay)
                    
                    success_count += 1
                    
            except Exception as e:
                logger.error(f"Error batch both file {i}: {e}")
                error_count += 1
        
        await status.edit_text(
            f"✅ Processing complete!\n\n"
            f"Successful: {success_count}\n"
            f"Errors: {error_count}"
        )
        
    finally:
        clear_user_batch(user_id)
        session.pop('batch_mode', None)

async def process_batch_add_banner(client: Client, message: Message, user_id: int):
    """Add banner to all PDFs in batch"""
    # Import banner functions from core
    from link_bot.core import _ensure_banner_pdf_path, add_banner_pages_to_pdf
    
    if user_id not in user_batches:
        user_batches[user_id] = await db.get_batch_files(user_id)
    
    files = user_batches[user_id]
    pdf_files = [f for f in files if is_pdf_file(f['file_name'])]
    
    if not pdf_files:
        await client.send_message(message.chat.id, "❌ No PDF files in batch")
        return
    
    banner_pdf = await _ensure_banner_pdf_path(user_id)
    if not banner_pdf:
        await client.send_message(message.chat.id, "❌ No default banner. Use /setbanner first.")
        return
    
    session = ensure_session_dict(user_id)
    status = await create_or_edit_status(client, message, f"⏳ Adding banner to {len(pdf_files)} PDF files...")
    success_count = 0
    error_count = 0
    
    try:
        for i, file_info in enumerate(pdf_files):
            try:
                await status.edit_text(f"⏳ Processing file {i+1}/{len(pdf_files)}...")
                
                # Download file
                file_path = await client.download_media(
                    file_info['file_id'],
                    file_name=f"{get_user_temp_dir(user_id)}/batch_{i}.pdf"
                )
                
                with tempfile.TemporaryDirectory() as temp_dir:
                    input_path = Path(temp_dir) / "input.pdf"
                    output_path = Path(temp_dir) / file_info['file_name']
                    shutil.move(file_path, input_path)
                    
                    # Add banner
                    await add_banner_pages_to_pdf(str(input_path), str(output_path), banner_pdf, place='after')
                    
                    # Send file with banner
                    new_file_name = build_final_filename(user_id, file_info['file_name'])
                    delay = session.get('delete_delay', 300)
                    await send_and_delete(client, message.chat.id, output_path, new_file_name, delay_seconds=delay)
                    
                    success_count += 1
                    
            except Exception as e:
                logger.error(f"Error batch add banner file {i}: {e}")
                error_count += 1
        
        await status.edit_text(
            f"✅ Processing complete!\n\n"
            f"Successful: {success_count}\n"
            f"Errors: {error_count}"
        )
        
    finally:
        clear_user_batch(user_id)
        session.pop('batch_mode', None)

async def process_batch_lock(client: Client, message: Message, user_id: int, password: str):
    """Lock all PDFs in batch"""
    from link_bot.core import lock_pdf_with_password
    
    if user_id not in user_batches:
        user_batches[user_id] = await db.get_batch_files(user_id)
    
    files = user_batches[user_id]
    pdf_files = [f for f in files if is_pdf_file(f['file_name'])]
    
    if not pdf_files:
        await client.send_message(message.chat.id, "❌ No PDF files in batch")
        return
    
    # Get default password if needed
    if not password or password.strip().lower() == 'default':
        settings = await db.get_user_settings(user_id)
        password = settings.get('lock_password')
    
    if not password:
        await client.send_message(message.chat.id, "ℹ️ No password provided — proceeding without lock.")
        return
    
    session = ensure_session_dict(user_id)
    status = await create_or_edit_status(client, message, f"⏳ Locking {len(pdf_files)} PDF files...")
    success_count = 0
    error_count = 0
    
    try:
        for i, file_info in enumerate(pdf_files):
            try:
                await status.edit_text(f"⏳ Processing file {i+1}/{len(pdf_files)}...")
                
                # Download file
                file_path = await client.download_media(
                    file_info['file_id'],
                    file_name=f"{get_user_temp_dir(user_id)}/batch_{i}.pdf"
                )
                
                with tempfile.TemporaryDirectory() as temp_dir:
                    input_path = Path(temp_dir) / "input.pdf"
                    output_path = Path(temp_dir) / f"locked_{file_info['file_name']}"
                    shutil.move(file_path, input_path)
                    
                    # Lock PDF
                    lock_pdf_with_password(str(input_path), str(output_path), password)
                    
                    # Send locked file
                    new_file_name = build_final_filename(user_id, file_info['file_name'])
                    delay = session.get('delete_delay', 300)
                    await send_and_delete(client, message.chat.id, output_path, new_file_name, delay_seconds=delay)
                    
                    success_count += 1
                    
            except Exception as e:
                logger.error(f"Error batch lock file {i}: {e}")
                error_count += 1
        
        await status.edit_text(
            f"✅ Processing complete!\n\n"
            f"Successful: {success_count}\n"
            f"Errors: {error_count}"
        )
        
    finally:
        clear_user_batch(user_id)
        session.pop('batch_mode', None)

# Callback handlers for batch operations
@Client.on_callback_query(filters.regex(r"^batch_"))
async def handle_batch_callbacks(client: Client, query: CallbackQuery):
    """Handle all batch-related callbacks"""
    await query.answer()
    
    data = query.data
    parts = data.split(":")
    action = parts[0]
    user_id = int(parts[1]) if len(parts) > 1 else query.from_user.id
    
    # Verify user
    if query.from_user.id != user_id:
        await query.answer("❌ This is not for you!", show_alert=True)
        return
    
    session = ensure_session_dict(user_id)
    
    # Handle different batch actions
    if action == "batch_clear":
        clear_user_batch(user_id)
        await query.edit_message_text("🧹 Batch cleared successfully!")
    
    elif action == "batch_unlock":
        session['batch_action'] = 'unlock'
        session['awaiting_batch_password'] = True
        await query.edit_message_text("🔐 Send me the password for all PDFs:")
    
    elif action == "batch_pages":
        session['batch_action'] = 'pages'
        await query.edit_message_text(
            "📝 **Remove Pages (Batch)**\n\n"
            "Choose a quick option or enter pages manually.",
            reply_markup=get_batch_pages_buttons(user_id)
        )
    
    elif action == "batch_both":
        session['batch_action'] = 'both'
        session['awaiting_batch_both_password'] = True
        await query.edit_message_text(
            "🛠️ **The Both - Batch**\n\n"
            "Step 1/2: Send me the password (or 'none' if not protected):"
        )
    
    elif action == "batch_add_banner":
        await process_batch_add_banner(client, query.message, user_id)
    
    elif action == "batch_lock":
        settings = await db.get_user_settings(user_id)
        password = settings.get('lock_password')
        await process_batch_lock(client, query.message, user_id, password)
    
    elif action == "batch_fullproc":
        session['batch_action'] = 'fullproc'
        session['awaiting_batch_fullproc_password'] = True
        await query.edit_message_text(
            "⚡ **Full Process - Batch**\n\n"
            "Step 1/3: Send unlock password (or 'none' if not protected):",
            parse_mode=ParseMode.MARKDOWN
        )
    
    # Pages selection handlers
    elif action == "batch_pages_first":
        await process_batch_pages(client, query.message, user_id, "first")
    elif action == "batch_pages_last":
        await process_batch_pages(client, query.message, user_id, "last")
    elif action == "batch_pages_middle":
        await process_batch_pages(client, query.message, user_id, "middle")
    elif action == "batch_pages_manual":
        session['awaiting_batch_pages'] = True
        await query.edit_message_text("📝 Send pages to remove (e.g. `1,3-5`) or `none`.")
    
    # The Both pages selection
    elif action == "batch_both_first":
        password = session.get('batch_both_password', '')
        await process_batch_both(client, query.message, user_id, password, "first")
    elif action == "batch_both_last":
        password = session.get('batch_both_password', '')
        await process_batch_both(client, query.message, user_id, password, "last")
    elif action == "batch_both_middle":
        password = session.get('batch_both_password', '')
        await process_batch_both(client, query.message, user_id, password, "middle")
    elif action == "batch_both_manual":
        session['awaiting_batch_both_pages'] = True
        await query.edit_message_text("📝 Send pages to remove (e.g. `1,3-5`) or `none`.")

@Client.on_message(filters.text & filters.private)
async def handle_batch_text_steps(client: Client, message: Message):
    """Handle interactive text steps for batch workflows."""
    user_id = message.from_user.id
    session = ensure_session_dict(user_id)

    # Unlock (all)
    if session.get('awaiting_batch_password'):
        session.pop('awaiting_batch_password', None)
        password = (message.text or '').strip()
        try:
            await message.delete()
        except Exception:
            pass
        await process_batch_unlock(client, message, user_id, password)
        return

    # Pages (all)
    if session.get('awaiting_batch_pages'):
        session.pop('awaiting_batch_pages', None)
        pages_spec = (message.text or '').strip()
        try:
            await message.delete()
        except Exception:
            pass
        await process_batch_pages(client, message, user_id, pages_spec)
        return

    # The Both - step 1 password
    if session.get('awaiting_batch_both_password'):
        session.pop('awaiting_batch_both_password', None)
        password = (message.text or '').strip()
        session['batch_both_password'] = password
        try:
            await message.delete()
        except Exception:
            pass
        await message.reply_text(
            "🛠️ **The Both - Batch**\n\nStep 2/2: Send pages to remove (e.g. `1,3-5`) or `none`.",
            parse_mode=ParseMode.MARKDOWN
        )
        session['awaiting_batch_both_pages'] = True
        return

    # The Both - step 2 pages
    if session.get('awaiting_batch_both_pages'):
        session.pop('awaiting_batch_both_pages', None)
        pages_spec = (message.text or '').strip()
        password = session.get('batch_both_password', '')
        session.pop('batch_both_password', None)
        try:
            await message.delete()
        except Exception:
            pass
        await process_batch_both(client, message, user_id, password, pages_spec)
        return

    # Full Process - step 1 unlock password
    if session.get('awaiting_batch_fullproc_password'):
        session.pop('awaiting_batch_fullproc_password', None)
        session['batch_fullproc_password'] = (message.text or '').strip()
        try:
            await message.delete()
        except Exception:
            pass
        await message.reply_text("**Full Process - Batch**\n\nStep 2/3: Pages to remove (e.g. `1,3-5`) or `none`.", parse_mode=ParseMode.MARKDOWN)
        session['awaiting_batch_fullproc_pages'] = True
        return

    # Full Process - step 2 pages
    if session.get('awaiting_batch_fullproc_pages'):
        session.pop('awaiting_batch_fullproc_pages', None)
        session['batch_fullproc_pages'] = (message.text or '').strip()
        try:
            await message.delete()
        except Exception:
            pass
        await message.reply_text("**Full Process - Batch**\n\nStep 3/3: Lock password (or `skip`).", parse_mode=ParseMode.MARKDOWN)
        session['awaiting_batch_fullproc_lock'] = True
        return

    # Full Process - step 3 lock password and execute
    if session.get('awaiting_batch_fullproc_lock'):
        session.pop('awaiting_batch_fullproc_lock', None)
        lock_pw = (message.text or '').strip()
        try:
            await message.delete()
        except Exception:
            pass

        files = user_batches.get(user_id) or await db.get_batch_files(user_id)
        pdf_files = [f for f in files if is_pdf_file(f.get('file_name', ''))]
        unlock_pw = session.pop('batch_fullproc_password', '')
        pages_text = session.pop('batch_fullproc_pages', 'none')

        # Parse pages
        from utils.helpers import parse_pages_spec
        pages_to_remove = [] if pages_text.strip().lower() in {'none', 'no', 'skip', '0'} else list(parse_pages_spec(pages_text))
        if lock_pw.lower() in {'skip', 'none', 'no'}:
            lock_pw = ''

        await execute_batch_full_pipeline(client, message, user_id, pdf_files, unlock_pw, pages_to_remove, lock_pw)
        return

async def execute_batch_full_pipeline(client: Client, message: Message, user_id: int,
                                      files: List[Dict], unlock_pw: str,
                                      pages_to_remove: List[int], lock_pw: str):
    """Execute Full Process pipeline for all PDFs in batch."""
    if not files:
        await message.reply_text("❌ No PDF files in batch")
        return

    session = ensure_session_dict(user_id)
    status = await create_or_edit_status(client, message, f"⏳ Full Process on {len(files)} files...")
    success = 0
    errors = 0

    try:
        for i, file_info in enumerate(files):
            try:
                await status.edit_text(f"⏳ Processing file {i+1}/{len(files)}...")

                # Download
                file_path = await client.download_media(
                    file_info['file_id'],
                    file_name=f"{get_user_temp_dir(user_id)}/batch_{i}.pdf"
                )

                with tempfile.TemporaryDirectory() as temp_dir:
                    current = Path(temp_dir) / "input.pdf"
                    shutil.move(file_path, current)

                    # 1. Unlock
                    if unlock_pw and unlock_pw.lower() != 'none':
                        unlocked = Path(temp_dir) / "unlocked.pdf"
                        with pikepdf.open(str(current), password=unlock_pw) as pdf:
                            pdf.save(str(unlocked))
                        current = unlocked

                    # 2. Clean banners
                    with open(current, 'rb') as f:
                        pdf_bytes = f.read()
                    cleaned_bytes = clean_pdf_banners(pdf_bytes, user_id)
                    cleaned = Path(temp_dir) / "cleaned.pdf"
                    with open(cleaned, 'wb') as f:
                        f.write(cleaned_bytes)
                    current = cleaned

                    # 3. Add banner
                    banner_pdf = await _ensure_banner_pdf_path(user_id)
                    if not banner_pdf:
                        from link_bot.core import create_default_banner_pdf
                        banner_pdf = create_default_banner_pdf(user_id)
                    if banner_pdf:
                        bannered = Path(temp_dir) / "bannered.pdf"
                        await add_banner_pages_to_pdf(str(current), str(bannered), banner_pdf, 'after')
                        current = bannered

                    # 4. Remove pages
                    if pages_to_remove:
                        paged = Path(temp_dir) / "paged.pdf"
                        remove_pages_by_numbers(str(current), str(paged), pages_to_remove)
                        current = paged

                    # 5. Lock
                    if lock_pw:
                        locked = Path(temp_dir) / "locked.pdf"
                        lock_pdf_with_password(str(current), str(locked), lock_pw)
                        current = locked

                    # Send
                    new_name = build_final_filename(user_id, file_info['file_name'])
                    delay = session.get('delete_delay', 300)
                    await send_and_delete(client, message.chat.id, str(current), new_name, delay_seconds=delay)
                    success += 1

            except Exception as e:
                logger.error(f"Batch Full Process error on file {i}: {e}")
                errors += 1

        await status.edit_text(
            f"✅ Full Process complete!\n\nSuccessful: {success}\nErrors: {errors}"
        )
    finally:
        clear_user_batch(user_id)
        session.pop('batch_mode', None)
