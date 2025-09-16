"""
Helper functions for PDF Bot
Common utilities used across all modules
"""
import os
import re
import tempfile
import asyncio
import logging
from pathlib import Path
from typing import List, Dict, Optional, Set, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)

# Global constants
TEMP_DIR = Path("temp_files")
TEMP_DIR.mkdir(exist_ok=True)

def get_user_temp_dir(user_id: int) -> Path:
    """Get or create user's temporary directory"""
    user_dir = TEMP_DIR / str(user_id)
    user_dir.mkdir(exist_ok=True)
    return user_dir

def clean_filename(filename: str) -> str:
    """Clean filename by removing usernames, hashtags and emojis"""
    # Remove blocks containing @ or #
    cleaned = re.sub(r'[\[\(\{\<][^)\]\}\>]*[@#][^)\]\}\>]*[\]\)\}\>]', '', filename)
    
    # Remove standalone usernames
    cleaned = re.sub(r'@[_A-Za-z0-9]+', '', cleaned)
    
    # Remove hashtags
    cleaned = re.sub(r'#\w+', '', cleaned)
    
    # Remove emojis
    emoji_pattern = re.compile(
        "["
        u"\U0001F600-\U0001F64F"  # emoticons
        u"\U0001F300-\U0001F5FF"  # symbols & pictographs
        u"\U0001F680-\U0001F6FF"  # transport & map symbols
        u"\U0001F1E0-\U0001F1FF"  # flags (iOS)
        u"\U00002700-\U000027BF"
        u"\U000024C2-\U0001F251"
        "]+", flags=re.UNICODE)
    cleaned = emoji_pattern.sub(r'', cleaned)
    
    # Clean empty parentheses/brackets
    cleaned = re.sub(r'[\[\(\{\<]\s*[\]\)\}\>]', '', cleaned)
    
    # Clean multiple spaces
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    
    return cleaned

def clean_caption_with_username(original_caption: str, user_id: int = None) -> str:
    """Clean caption and add user's saved username"""
    from utils.sessions import sessions
    from utils.database import db
    
    # Remove existing usernames
    cleaned = re.sub(r"@[\w_]+", "", original_caption).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    
    if user_id:
        # Get username from session or database
        session = sessions.get(user_id, {})
        username = session.get('username')
        
        if not username:
            # Try to get from database (async to sync bridge needed)
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                settings = loop.run_until_complete(db.get_user_settings(user_id))
                username = settings.get('username')
            except Exception:
                pass
        
        if username:
            # Get text position preference
            pos = session.get('text_position', 'end')
            if pos == 'start':
                return f"{username} {cleaned}".strip()
            else:
                return f"{cleaned} {username}".strip()
    
    return cleaned

def build_final_filename(user_id: int, original_name: str) -> str:
    """Build final filename with user's tag"""
    try:
        base, ext = os.path.splitext(original_name)
        if not ext:
            ext = ".pdf"
        
        # Clean base name
        base = clean_filename(base)
        
        # Get user's tag
        from utils.sessions import sessions
        from utils.database import db
        
        session = sessions.get(user_id, {})
        username = session.get('username')
        
        if not username:
            # Try database
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                settings = loop.run_until_complete(db.get_user_settings(user_id))
                username = settings.get('username')
            except Exception:
                pass
        
        if not username:
            # Sanitize and return
            safe_base = re.sub(r'[\\/:*?"<>|]', '_', base).strip()
            return f"{safe_base}{ext}"
        
        # Get position preference
        pos = session.get('text_position', 'end')
        if pos == 'start':
            new_base = f"{username} {base}".strip()
        else:
            new_base = f"{base} {username}".strip()
        
        # Sanitize
        new_base = re.sub(r'[\\/:*?"<>|]', '_', new_base)
        return f"{new_base}{ext}"
        
    except Exception as e:
        logger.error(f"build_final_filename error: {e}")
        return original_name

def is_pdf_file(filename: str) -> bool:
    """Check if file is a PDF"""
    return filename.lower().endswith('.pdf')

def is_supported_video(filename: str) -> bool:
    """Check if file is a supported video"""
    import mimetypes
    mimetype, _ = mimetypes.guess_type(filename)
    return mimetype and mimetype.startswith("video/")

def parse_pages_spec(spec: str) -> List[int]:
    """Parse page specification string (e.g., '1,3-5,7')"""
    spec = (spec or "").strip().lower()
    if not spec or spec in {"none", "0", "no", "non", "skip"}:
        return []
    
    pages: Set[int] = set()
    for chunk in spec.replace(" ", "").split(","):
        if not chunk:
            continue
        if "-" in chunk:
            try:
                a, b = chunk.split("-", 1)
                if a.isdigit() and b.isdigit():
                    a_i, b_i = int(a), int(b)
                    if a_i <= b_i:
                        pages.update(range(a_i, b_i + 1))
            except Exception:
                pass
        elif chunk.isdigit():
            pages.add(int(chunk))
    
    return sorted(p for p in pages if p >= 1)

def parse_pages_text(text: str) -> Tuple[List[int], Optional[str]]:
    """Parse user-provided pages string with error handling"""
    spec = (text or "").strip()
    if not spec:
        return [], None
    
    # Quick validation
    if not re.fullmatch(r"[\d,\-\s]+", spec):
        return [], "Invalid format. Use numbers, commas and dashes (e.g. 1,3-5)."
    
    pages = parse_pages_spec(spec)
    if not pages and spec and spec not in {"0", "none", "no", "non", "skip"}:
        return [], "No valid pages found. Example: 1,3-5"
    
    return pages, None

def format_bytes(n: int) -> str:
    """Format bytes to human readable string"""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"

def format_uptime(seconds: float) -> str:
    """Format seconds to uptime string"""
    seconds = int(seconds)
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}h{m:02d}m{s:02d}s"

async def send_and_delete(client, chat_id: int, file_path: str, file_name: str, 
                          caption: str = None, delay_seconds: int = 300):
    """Send document and auto-delete after delay"""
    try:
        # Send document
        with open(file_path, 'rb') as f:
            sent = await client.send_document(
                chat_id,
                document=f,
                file_name=file_name,
                caption=caption or ""
            )
        
        logger.info(f"✅ Document sent: {file_name}")
        
        # Update stats
        from utils.database import db
        file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        await db.bump_stats(file_size)
        
        # Schedule deletion
        if delay_seconds > 0:
            async def delete_after_delay():
                await asyncio.sleep(delay_seconds)
                try:
                    await sent.delete()
                    logger.info(f"Message deleted after {delay_seconds}s")
                except Exception as e:
                    logger.error(f"Error deleting message: {e}")
                
                # Delete local file
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        logger.info(f"Local file deleted: {file_path}")
                except Exception as e:
                    logger.error(f"Error deleting file: {e}")
            
            asyncio.create_task(delete_after_delay())
        
    except Exception as e:
        logger.error(f"Error in send_and_delete: {e}")
        raise

async def create_or_edit_status(client, origin, text: str):
    """Create a new status message"""
    # Resolve chat id from origin
    chat_id = None
    try:
        chat_id = origin.chat.id
    except Exception:
        try:
            chat_id = origin.message.chat.id
        except Exception:
            chat_id = None
    
    if chat_id is None:
        logger.error("create_or_edit_status: unable to resolve chat_id")
        raise ValueError("Cannot resolve chat_id for status message")
    
    try:
        return await client.send_message(chat_id, text)
    except Exception as e:
        logger.error(f"create_or_edit_status error: {e}")
        raise

async def safe_edit_message(target, text: str, reply_markup=None):
    """Edit message safely, avoiding MESSAGE_NOT_MODIFIED errors"""
    try:
        # Check if it's a CallbackQuery
        edit_cb = getattr(target, "edit_message_text", None)
        if callable(edit_cb):
            if reply_markup is not None:
                await edit_cb(text, reply_markup=reply_markup)
            else:
                await edit_cb(text)
            return
        
        # Fallback for Message objects
        if reply_markup is not None:
            await target.edit_text(text, reply_markup=reply_markup)
        else:
            await target.edit_text(text)
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" in str(e):
            logger.debug(f"Message not modified: {e}")
        else:
            logger.error(f"Error editing message: {e}")
            raise

# Rate limiting and duplicate detection
processed_messages: Dict[str, datetime] = {}
user_last_command: Dict[int, Tuple[str, datetime]] = {}
user_actions: Dict[int, List[datetime]] = {}

def check_rate_limit(user_id: int, batch_mode: bool = False) -> bool:
    """Check if user is within rate limits"""
    from utils.sessions import sessions
    
    # Higher limit for batch mode
    rate_limit = 100 if batch_mode else 30
    
    current_time = datetime.now()
    
    # Clean old actions
    user_actions[user_id] = [
        t for t in user_actions.get(user_id, [])
        if (current_time - t).seconds < 60
    ]
    
    if len(user_actions.get(user_id, [])) >= rate_limit:
        logger.warning(f"⚠️ Rate limit reached for user {user_id}")
        return False
    
    user_actions.setdefault(user_id, []).append(current_time)
    return True

def is_duplicate_message(user_id: int, message_id: int, command_type: str = "message") -> str:
    """Check for duplicate messages"""
    current_time = datetime.now()
    
    # Check rate limit
    from utils.sessions import sessions
    session = sessions.get(user_id, {})
    batch_mode = session.get('batch_mode', False)
    
    if not check_rate_limit(user_id, batch_mode):
        return "rate_limit"
    
    # Check repeated commands
    if command_type in ["start", "batch", "process"]:
        if user_id in user_last_command:
            last_cmd, last_time = user_last_command[user_id]
            if last_cmd == command_type and (current_time - last_time).total_seconds() < 2:
                logger.info(f"Command {command_type} ignored - repeated too quickly")
                return "duplicate"
        user_last_command[user_id] = (command_type, current_time)
    
    # Check message ID
    key = f"{user_id}_{message_id}"
    
    # Clean old messages
    keys_to_remove = []
    for k, timestamp in processed_messages.items():
        if (current_time - timestamp).seconds > 300:
            keys_to_remove.append(k)
    for k in keys_to_remove:
        del processed_messages[k]
    
    # Check duplicate
    if key in processed_messages:
        return "duplicate"
    
    processed_messages[key] = current_time
    return ""

async def send_limit_message(client, chat_id: int, limit_type: str):
    """Send rate limit or duplicate message"""
    if limit_type == "rate_limit":
        await client.send_message(
            chat_id,
            "⛔️ Limit reached: You can only send 30 files per minute.\n\n"
            "⏰ Try again in a few seconds."
        )
    elif limit_type == "duplicate":
        await client.send_message(
            chat_id,
            "⚠️ Duplicate file: This file was processed recently.\n\n"
            "⏰ Please wait 5 minutes before sending it again."
        )
