"""
Scribd document downloader for PDF Bot
Handles downloading documents from Scribd links
"""
import os
import subprocess
import logging
import tempfile
from pathlib import Path
from typing import Optional

from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery
from pyrogram.enums import ParseMode

from utils.database import db
from utils.sessions import ensure_session_dict
from utils.helpers import get_user_temp_dir
from link_bot.admin import is_user_in_channel, send_force_join_message

logger = logging.getLogger(__name__)

# Downloads directory
DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

def is_scribd_url(url: str) -> bool:
    """Check if URL is from Scribd"""
    return "scribd.com" in url.lower()

async def download_from_scribd(url: str, output_dir: str = "downloads") -> Optional[str]:
    """
    Download a document from Scribd using scribd-downloader
    Returns the path to the downloaded file or None if failed
    """
    os.makedirs(output_dir, exist_ok=True)
    
    try:
        # Use scribd-downloader tool
        # Install with: pip install scribd-downloader
        result = subprocess.run(
            ["scribdl", "-i", url],
            cwd=output_dir,
            capture_output=True,
            text=True,
            timeout=60
        )
        
        if result.returncode != 0:
            logger.error(f"Scribd download failed: {result.stderr}")
            return None
        
        # Find the downloaded file
        for fname in os.listdir(output_dir):
            if fname.lower().endswith((".pdf", ".txt")):
                return os.path.join(output_dir, fname)
        
        return None
        
    except subprocess.TimeoutExpired:
        logger.error("Scribd download timed out")
        return None
    except Exception as e:
        logger.error(f"Error downloading from Scribd: {e}")
        return None

async def download_from_scribd_playwright(url: str, output_dir: str = "downloads") -> Optional[str]:
    """
    Alternative method using Playwright for Scribd downloads
    More robust but requires more setup
    """
    try:
        # Import here to avoid dependency if not needed
        from playwright.async_api import async_playwright
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            
            # Navigate to Scribd URL
            await page.goto(url, wait_until="networkidle", timeout=30000)
            
            # Try to extract document title
            title = await page.title()
            
            # Close popups if any
            try:
                close_button = page.locator("button:has-text('Close')")
                if await close_button.is_visible(timeout=1000):
                    await close_button.click()
            except Exception:
                pass
            
            # Scroll to load all pages
            for _ in range(10):
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await page.wait_for_timeout(500)
            
            # Take screenshots of pages (fallback method)
            output_path = Path(output_dir) / f"{title}.pdf"
            
            # Get all page containers
            pages = await page.query_selector_all(".page_container")
            
            if not pages:
                logger.error("No pages found on Scribd")
                await browser.close()
                return None
            
            # Create PDF from screenshots
            from PIL import Image
            images = []
            
            for i, page_elem in enumerate(pages):
                # Screenshot each page
                screenshot = await page_elem.screenshot()
                
                # Convert to PIL Image
                import io
                img = Image.open(io.BytesIO(screenshot))
                images.append(img)
            
            # Save as PDF
            if images:
                images[0].save(
                    output_path, 
                    "PDF",
                    resolution=100.0,
                    save_all=True,
                    append_images=images[1:]
                )
                
                await browser.close()
                return str(output_path)
            
            await browser.close()
            return None
            
    except Exception as e:
        logger.error(f"Error with Playwright Scribd download: {e}")
        return None

@Client.on_callback_query(filters.regex("^download_link$"))
async def download_link_callback(client: Client, query: CallbackQuery):
    """Handle download link button click"""
    user_id = query.from_user.id
    
    # Check force join
    if not await is_user_in_channel(client, user_id):
        await send_force_join_message(client, query.message)
        return
    
    session = ensure_session_dict(user_id)
    session['awaiting_download_url'] = True
    
    await query.edit_message_text(
        "🔗 **Send me the link to download**\n\n"
        "Supported:\n"
        "• Scribd documents\n"
        "• Direct PDF links\n\n"
        "Send /cancel to abort.",
        parse_mode=ParseMode.MARKDOWN
    )

@Client.on_message(filters.text & filters.private)
async def handle_download_url(client: Client, message: Message):
    """Handle URL download requests"""
    user_id = message.from_user.id
    session = ensure_session_dict(user_id)
    
    # Check if awaiting download URL
    if not session.get('awaiting_download_url'):
        return
    
    session.pop('awaiting_download_url', None)
    url = message.text.strip()
    
    # Basic URL validation
    if not (url.startswith('http://') or url.startswith('https://')):
        await message.reply_text("❌ Please send a valid URL starting with http:// or https://")
        return
    
    # Handle Scribd URLs
    if is_scribd_url(url):
        await handle_scribd_download(client, message, url)
        return
    
    # Handle direct PDF links
    if url.lower().endswith('.pdf'):
        try:
            await message.reply_text("📥 Downloading PDF...")
            await client.send_document(
                chat_id=message.chat.id,
                document=url,
                caption="📄 Downloaded PDF"
            )
        except Exception as e:
            logger.error(f"Direct PDF download failed: {e}")
            await message.reply_text("❌ Couldn't download the file from the URL.")
        return
    
    # Try to download as generic document
    try:
        await message.reply_text("📥 Attempting download...")
        await client.send_document(
            chat_id=message.chat.id,
            document=url,
            caption="📄 Downloaded document"
        )
    except Exception as e:
        logger.error(f"Generic download failed: {e}")
        await message.reply_text("❌ Couldn't download the file. Make sure it's a valid document URL.")

async def handle_scribd_download(client: Client, message: Message, url: str):
    """Handle Scribd document download"""
    user_id = message.from_user.id
    
    # Send processing message
    status = await message.reply_text("🔄 Downloading from Scribd, please wait...")
    
    try:
        # Create user-specific download directory
        user_dir = get_user_temp_dir(user_id)
        
        # Try primary download method
        file_path = await download_from_scribd(url, str(user_dir))
        
        # If primary fails, try Playwright method
        if not file_path:
            await status.edit_text("🔄 Trying alternative method...")
            file_path = await download_from_scribd_playwright(url, str(user_dir))
        
        if not file_path:
            await status.edit_text("❌ Failed to download the Scribd document.")
            return
        
        # Send the downloaded file
        await status.edit_text("📤 Sending document...")
        
        with open(file_path, 'rb') as f:
            await client.send_document(
                message.chat.id,
                document=f,
                caption=f"📄 Scribd Document\n🔗 {url[:50]}{'...' if len(url) > 50 else ''}"
            )
        
        # Update stats
        file_size = os.path.getsize(file_path)
        await db.bump_stats(file_size)
        
        # Clean up
        try:
            os.remove(file_path)
        except Exception:
            pass
        
        await status.delete()
        
    except Exception as e:
        logger.error(f"Scribd download error: {e}")
        await status.edit_text(f"❌ Error downloading from Scribd: {str(e)[:200]}")

# Auto-detect Scribd URLs in messages
@Client.on_message(filters.regex(r"scribd\.com/document/") & filters.private)
async def auto_detect_scribd(client: Client, message: Message):
    """Auto-detect and process Scribd URLs"""
    user_id = message.from_user.id
    
    # Track user
    await db.track_user(user_id)
    
    # Check force join
    if not await is_user_in_channel(client, user_id):
        await send_force_join_message(client, message)
        return
    
    # Extract URL
    url = message.text.strip()
    
    # Process Scribd URL
    await handle_scribd_download(client, message, url)

# Scribd batch download support
async def download_scribd_batch(client: Client, urls: list, user_id: int) -> list:
    """Download multiple Scribd documents"""
    downloaded_files = []
    user_dir = get_user_temp_dir(user_id)
    
    for i, url in enumerate(urls):
        if not is_scribd_url(url):
            continue
        
        try:
            # Download each document
            file_path = await download_from_scribd(url, str(user_dir))
            
            if file_path:
                downloaded_files.append({
                    'path': file_path,
                    'url': url,
                    'index': i
                })
        except Exception as e:
            logger.error(f"Error downloading Scribd batch item {i}: {e}")
            continue
    
    return downloaded_files

# Integration with manga/webtoon module if available
try:
    from handlers.manga_handler import process_manga_url
    
    @Client.on_message(filters.regex(r"scribd\.com") & filters.private)
    async def handle_scribd_with_manga(client: Client, message: Message):
        """Handle Scribd URLs with manga module integration"""
        user_id = message.from_user.id
        url = message.text.strip()
        
        # Check if it's a comic/manga on Scribd
        if any(word in url.lower() for word in ['comic', 'manga', 'webtoon']):
            try:
                # Use manga handler for comics
                await process_manga_url(client, message.chat.id, user_id, url, then_edit=False)
                return
            except Exception:
                # Fall back to regular Scribd download
                pass
        
        # Regular Scribd download
        await handle_scribd_download(client, message, url)
        
except ImportError:
    # Manga handler not available
    pass

# Helper function for other modules
async def download_document(url: str, user_id: int) -> Optional[str]:
    """
    Generic document download function for use by other modules
    Returns path to downloaded file or None
    """
    user_dir = get_user_temp_dir(user_id)
    
    if is_scribd_url(url):
        return await download_from_scribd(url, str(user_dir))
    
    # Try direct download for other URLs
    try:
        import aiohttp
        import aiofiles
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    # Generate filename from URL
                    filename = url.split('/')[-1] or "document.pdf"
                    filepath = user_dir / filename
                    
                    # Save file
                    async with aiofiles.open(filepath, 'wb') as f:
                        await f.write(await response.read())
                    
                    return str(filepath)
    except Exception as e:
        logger.error(f"Generic download error: {e}")
    
    return None

# Export functions for use by other modules
__all__ = [
    'download_from_scribd',
    'download_document',
    'is_scribd_url',
    'handle_scribd_download',
    'download_scribd_batch'
]
