"""
Scribd document downloader for PDF Bot
Handles downloading documents from Scribd links
"""
import os
import subprocess
import logging
import tempfile
from pathlib import Path
from typing import Optional, List, Tuple

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
    Legacy external tool method (disabled on Windows environment by default).
    Returns None to fall back to Playwright.
    """
    return None

async def _collect_page_elements(page) -> List[Tuple[object, float]]:
    """Collect probable Scribd page nodes across main page and iframes.
    Returns list of tuples (element_handle, y_position) sorted by y.
    """
    selectors = [
        ".page_container",
        ".outer_page",
        "[data-page-number]",
        ".page",
        ".text_layer",
        "canvas.page_canvas",
        "canvas",
    ]
    elements: List[Tuple[object, float]] = []

    async def add_from_context(ctx):
        for sel in selectors:
            try:
                nodes = await ctx.query_selector_all(sel)
            except Exception:
                nodes = []
            for n in nodes:
                try:
                    box = await n.bounding_box()
                    if box and box.get("height", 0) > 20:
                        elements.append((n, box.get("y", 0.0)))
                except Exception:
                    # If bounding box fails, still include with default ordering
                    elements.append((n, float("inf")))

    # Main page
    await add_from_context(page)

    # Iframes
    try:
        for f in page.frames:
            try:
                await add_from_context(f)
            except Exception:
                continue
    except Exception:
        pass

    # Sort by Y position (top to bottom)
    elements.sort(key=lambda t: t[1])
    # Deduplicate by element id; ElementHandle has a unique id in repr
    seen = set()
    dedup: List[Tuple[object, float]] = []
    for el, y in elements:
        key = repr(el)
        if key in seen:
            continue
        seen.add(key)
        dedup.append((el, y))
    return dedup

async def download_from_scribd_playwright(url: str, output_dir: str = "downloads") -> Optional[str]:
    """
    Playwright-based Scribd downloader with robust element detection and fallbacks.
    """
    try:
        from playwright.async_api import async_playwright
        try:
            from playwright_stealth import stealth_async
        except Exception:
            stealth_async = None
        
        os.makedirs(output_dir, exist_ok=True)
        headless_env = os.getenv("SCRIBD_HEADLESS", "1").strip()
        headless = headless_env not in {"0", "false", "False"}
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless)
            context = await browser.new_context()
            page = await context.new_page()

            if stealth_async is not None:
                try:
                    await stealth_async(page)
                except Exception:
                    pass

            page.set_default_navigation_timeout(120000)
            page.set_default_timeout(120000)

            await page.goto(url, wait_until="domcontentloaded", timeout=90000)
            try:
                await page.wait_for_load_state("networkidle", timeout=60000)
            except Exception:
                pass

            raw_title = (await page.title()) or "scribd_document"
            safe_title = "".join(c for c in raw_title if c not in '\\/:*?"<>|').strip() or "scribd_document"
            output_path = Path(output_dir) / f"{safe_title}.pdf"

            # Try to accept cookies or close popups
            for selector in ["button:has-text('Accept')", "button:has-text('Got it')", "button:has-text('Close')"]:
                try:
                    btn = page.locator(selector)
                    if await btn.is_visible(timeout=1000):
                        await btn.click()
                except Exception:
                    pass

            # Scrolling to load content
            for _ in range(30):
                await page.evaluate("window.scrollBy(0, Math.floor(window.innerHeight*0.9))")
                await page.wait_for_timeout(400)

            # Try to collect elements
            elements = await _collect_page_elements(page)
            if not elements:
                logger.error("No page-like elements found; falling back to full-page screenshot")
                # Full page screenshot fallback -> single-page PDF
                from PIL import Image
                import io
                shot = await page.screenshot(full_page=True)
                img = Image.open(io.BytesIO(shot)).convert("RGB")
                img.save(output_path, "PDF", resolution=100.0)
                img.close()
                await browser.close()
                return str(output_path)

            # Screenshot each element
            from PIL import Image
            import io
            images = []
            for idx, (elem, _) in enumerate(elements):
                try:
                    b = await elem.bounding_box()
                    if not b or b.get("height", 0) < 20:
                        continue
                    # Ensure element in viewport
                    try:
                        await elem.scroll_into_view_if_needed(timeout=5000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(150)
                    shot = await elem.screenshot()
                    img = Image.open(io.BytesIO(shot)).convert("RGB")
                    images.append(img)
                except Exception as e:
                    logger.error(f"Scribd element screenshot failed at index {idx}: {e}")
                    continue

            if not images:
                await browser.close()
                return None

            # Save as PDF
            first, rest = images[0], images[1:]
            first.save(output_path, "PDF", resolution=100.0, save_all=True, append_images=rest)
            for im in images:
                try:
                    im.close()
                except Exception:
                    pass

            await browser.close()
            return str(output_path)

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
    
    # Try to download any direct file (PDF or other) to avoid Telegram URL fetch issues
    try:
        await message.reply_text("📥 Downloading...")
        local_path = await download_document(url, user_id)
        if local_path and os.path.exists(local_path):
            with open(local_path, 'rb') as f:
                await client.send_document(
                    chat_id=message.chat.id,
                    document=f,
                    caption="📄 Downloaded document"
                )
            try:
                os.remove(local_path)
            except Exception:
                pass
            return
    except Exception as e:
        logger.error(f"Generic download (fetch) failed: {e}")
    
    # Fallback: let Telegram try to fetch
    try:
        await client.send_document(
            chat_id=message.chat.id,
            document=url,
            caption="📄 Downloaded document"
        )
    except Exception as e:
        logger.error(f"Generic download failed: {e}")
        await message.reply_text("❌ Couldn't download the file. Make sure it's a direct file link or use supported sources.")

async def handle_scribd_download(client: Client, message: Message, url: str):
    """Handle Scribd document download"""
    user_id = message.from_user.id
    
    # Send processing message
    status = await message.reply_text("🔄 Downloading from Scribd, please wait...")
    
    try:
        # Create user-specific download directory
        user_dir = get_user_temp_dir(user_id)
        
        # Prefer Playwright method
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
            file_path = await download_from_scribd_playwright(url, str(user_dir))
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
        return await download_from_scribd_playwright(url, str(user_dir))
    
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
